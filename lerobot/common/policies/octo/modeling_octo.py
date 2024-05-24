#!/usr/bin/env python

# Copyright 2024 Columbia Artificial Intelligence, Robotics Lab,
# and The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Diffusion Policy as per "Diffusion Policy: Visuomotor Policy Learning via Action Diffusion"

TODO(alexander-soare):
  - Remove reliance on diffusers for DDPMScheduler and LR scheduler.
  - Make compatible with multiple image keys.
"""

from collections import deque
from typing import Callable

import einops
import torch
import torch.nn.functional as F  # noqa: N812
import torchvision
from diffusers.schedulers.scheduling_ddim import DDIMScheduler
from diffusers.schedulers.scheduling_ddpm import DDPMScheduler
from einops import rearrange, repeat
from huggingface_hub import PyTorchModelHubMixin
from torch import Tensor, nn
from torch.nn import TransformerEncoder, TransformerEncoderLayer

from lerobot.common.policies.normalize import Normalize, Unnormalize
from lerobot.common.policies.octo.components import (
    FourierFeatures,
    MLPResNet,
    PositionalEncoding,
    TimeMLP,
)
from lerobot.common.policies.octo.configuration_octo import OctoConfig
from lerobot.common.policies.utils import (
    get_device_from_parameters,
    get_dtype_from_parameters,
    populate_queues,
)


class OctoPolicy(nn.Module, PyTorchModelHubMixin):
    """
    Diffusion Policy as per "Diffusion Policy: Visuomotor Policy Learning via Action Diffusion"
    (paper: https://arxiv.org/abs/2303.04137, code: https://github.com/real-stanford/diffusion_policy).
    """

    name = "diffusion"

    def __init__(
        self,
        config: OctoConfig | None = None,
        dataset_stats: dict[str, dict[str, Tensor]] | None = None,
    ):
        """
        Args:
            config: Policy configuration class instance or None, in which case the default instantiation of
                the configuration class is used.
            dataset_stats: Dataset statistics to be used for normalization. If not passed here, it is expected
                that they will be passed with a call to `load_state_dict` before the policy is used.
        """
        super().__init__()
        if config is None:
            config = OctoConfig()
        self.config = config
        self.normalize_inputs = Normalize(
            config.input_shapes, config.input_normalization_modes, dataset_stats
        )
        self.normalize_targets = Normalize(
            config.output_shapes, config.output_normalization_modes, dataset_stats
        )
        self.unnormalize_outputs = Unnormalize(
            config.output_shapes, config.output_normalization_modes, dataset_stats
        )

        # queues are populated during rollout of the policy, they contain the n latest observations and actions
        self._queues = None

        self.octo = OctoModel(config)

        image_keys = [k for k in config.input_shapes if k.startswith("observation.image")]
        # Note: This check is covered in the post-init of the config but have a sanity check just in case.
        if len(image_keys) != 1:
            raise NotImplementedError(
                f"{self.__class__.__name__} only handles one image for now. Got image keys {image_keys}."
            )
        self.input_image_key = image_keys[0]

        self.reset()

    def reset(self):
        """Clear observation and action queues. Should be called on `env.reset()`"""
        self._queues = {
            "observation.image": deque(maxlen=self.config.n_obs_steps),
            "observation.state": deque(maxlen=self.config.n_obs_steps),
            "action": deque(maxlen=self.config.n_action_steps),
        }

    @torch.no_grad
    def select_action(self, batch: dict[str, Tensor]) -> Tensor:
        """Select a single action given environment observations.

        This method handles caching a history of observations and an action trajectory generated by the
        underlying diffusion model. Here's how it works:
          - `n_obs_steps` steps worth of observations are cached (for the first steps, the observation is
            copied `n_obs_steps` times to fill the cache).
          - The diffusion model generates `horizon` steps worth of actions.
          - `n_action_steps` worth of actions are actually kept for execution, starting from the current step.
        Schematically this looks like:
            ----------------------------------------------------------------------------------------------
            (legend: o = n_obs_steps, h = horizon, a = n_action_steps)
            |timestep            | n-o+1 | n-o+2 | ..... | n     | ..... | n+a-1 | n+a   | ..... |n-o+1+h|
            |observation is used | YES   | YES   | YES   | NO    | NO    | NO    | NO    | NO    | NO    |
            |action is generated | YES   | YES   | YES   | YES   | YES   | YES   | YES   | YES   | YES   |
            |action is used      | NO    | NO    | NO    | YES   | YES   | YES   | NO    | NO    | NO    |
            ----------------------------------------------------------------------------------------------
        Note that this means we require: `n_action_steps < horizon - n_obs_steps + 1`. Also, note that
        "horizon" may not the best name to describe what the variable actually means, because this period is
        actually measured from the first observation which (if `n_obs_steps` > 1) happened in the past.
        """
        batch = self.normalize_inputs(batch)
        batch["observation.image"] = batch[self.input_image_key]

        self._queues = populate_queues(self._queues, batch)

        if len(self._queues["action"]) == 0:
            # stack n latest observations from the queue
            batch = {k: torch.stack(list(self._queues[k]), dim=1) for k in batch if k in self._queues}
            actions = self.diffusion.generate_actions(batch)

            # TODO(rcadene): make above methods return output dictionary?
            actions = self.unnormalize_outputs({"action": actions})["action"]

            self._queues["action"].extend(actions.transpose(0, 1))

        action = self._queues["action"].popleft()
        return action

    def forward(self, batch: dict[str, Tensor]) -> dict[str, Tensor]:
        """Run the batch through the model and compute the loss for training or validation."""
        batch = self.normalize_inputs(batch)
        batch["observation.image"] = batch[self.input_image_key]
        batch = self.normalize_targets(batch)
        loss = self.diffusion.compute_loss(batch)
        return {"loss": loss}


def _make_noise_scheduler(name: str, **kwargs: dict) -> DDPMScheduler | DDIMScheduler:
    """
    Factory for noise scheduler instances of the requested type. All kwargs are passed
    to the scheduler.
    """
    if name == "DDPM":
        return DDPMScheduler(**kwargs)
    elif name == "DDIM":
        return DDIMScheduler(**kwargs)
    else:
        raise ValueError(f"Unsupported noise scheduler type {name}")


class OctoModel(nn.Module):
    def __init__(self, config: OctoConfig):
        super().__init__()
        self.config = config

        self.rgb_encoder = OctoRgbEncoder(config)
        feat_map_shape = self.rgb_encoder.feature_map_shape
        obs_seq_len = ((feat_map_shape[1] * feat_map_shape[2]) + 1) * config.n_obs_steps
        self.transformer = OctoNet(
            n_obs=config.n_obs_steps,
            qpos_dim=config.input_shapes["observation.state"][0],
            img_dim=feat_map_shape[0],
            embed_dim=config.embed_dim,
            n_layers=config.n_layers,
            n_heads=config.n_heads,
            d_ffn=config.d_ffn,
            obs_seq_len=obs_seq_len,
            dropout=config.dropout,
        )
        self.action_head = DiffusionActionHead(
            time_dim=config.time_dim,
            cond_dim=config.embed_dim,
            actions_dim=config.output_shapes["action"][0] * config.horizon,
            n_diffusion_head_layers=config.n_diffusion_head_layers,
        )

        self.noise_scheduler = _make_noise_scheduler(
            config.noise_scheduler_type,
            num_train_timesteps=config.num_train_timesteps,
            beta_start=config.beta_start,
            beta_end=config.beta_end,
            beta_schedule=config.beta_schedule,
            clip_sample=config.clip_sample,
            clip_sample_range=config.clip_sample_range,
            prediction_type=config.prediction_type,
        )

        if config.num_inference_steps is None:
            self.num_inference_steps = self.noise_scheduler.config.num_train_timesteps
        else:
            self.num_inference_steps = config.num_inference_steps

    # ========= inference  ============
    def conditional_sample(
        self, batch_size: int, global_cond: Tensor | None = None, generator: torch.Generator | None = None
    ) -> Tensor:
        device = get_device_from_parameters(self)
        dtype = get_dtype_from_parameters(self)

        # Sample prior.
        sample = torch.randn(
            size=(batch_size, self.config.horizon, self.config.output_shapes["action"][0]),
            dtype=dtype,
            device=device,
            generator=generator,
        )

        self.noise_scheduler.set_timesteps(self.num_inference_steps)

        for t in self.noise_scheduler.timesteps:
            # Predict model output.
            model_output = self.unet(
                sample,
                torch.full(sample.shape[:1], t, dtype=torch.long, device=sample.device),
                global_cond=global_cond,
            )
            # Compute previous image: x_t -> x_t-1
            sample = self.noise_scheduler.step(model_output, t, sample, generator=generator).prev_sample

        return sample

    def generate_actions(self, batch: dict[str, Tensor]) -> Tensor:
        """
        This function expects `batch` to have:
        {
            "observation.state": (B, n_obs_steps, state_dim)
            "observation.image": (B, n_obs_steps, C, H, W)
        }
        """
        batch_size, n_obs_steps = batch["observation.state"].shape[:2]
        assert n_obs_steps == self.config.n_obs_steps

        # Extract image feature (first combine batch and sequence dims).
        img_features = self.rgb_encoder(einops.rearrange(batch["observation.image"], "b n ... -> (b n) ..."))
        # Separate batch and sequence dims.
        img_features = einops.rearrange(img_features, "(b n) ... -> b n ...", b=batch_size)
        # Concatenate state and image features then flatten to (B, global_cond_dim).
        global_cond = torch.cat([batch["observation.state"], img_features], dim=-1).flatten(start_dim=1)

        # run sampling
        sample = self.conditional_sample(batch_size, global_cond=global_cond)

        # `horizon` steps worth of actions (from the first observation).
        actions = sample[..., : self.config.output_shapes["action"][0]]
        # Extract `n_action_steps` steps worth of actions (from the current observation).
        start = n_obs_steps - 1
        end = start + self.config.n_action_steps
        actions = actions[:, start:end]

        return actions

    def compute_loss(self, batch: dict[str, Tensor]) -> Tensor:
        """
        This function expects `batch` to have (at least):
        {
            "observation.state": (B, n_obs_steps, state_dim)
            "observation.image": (B, n_obs_steps, C, H, W)
            "action": (B, horizon, action_dim)
            "action_is_pad": (B, horizon)
        }
        """
        # Input validation.
        assert set(batch).issuperset({"observation.state", "observation.image", "action", "action_is_pad"})
        batch_size, n_obs_steps = batch["observation.state"].shape[:2]
        horizon = batch["action"].shape[1]
        assert horizon == self.config.horizon
        assert n_obs_steps == self.config.n_obs_steps

        # Extract image feature (first combine batch and sequence dims).
        img_features = self.rgb_encoder(einops.rearrange(batch["observation.image"], "b n ... -> (b n) ..."))
        # Separate batch and sequence dims.
        img_features = einops.rearrange(img_features, "(b n) ... -> b n ...", b=batch_size)
        # Concatenate state and image features then flatten to (B, global_cond_dim).
        global_cond = torch.cat([batch["observation.state"], img_features], dim=-1).flatten(start_dim=1)

        trajectory = batch["action"]

        # Forward diffusion.
        # Sample noise to add to the trajectory.
        eps = torch.randn(trajectory.shape, device=trajectory.device)
        # Sample a random noising timestep for each item in the batch.
        timesteps = torch.randint(
            low=0,
            high=self.noise_scheduler.config.num_train_timesteps,
            size=(trajectory.shape[0],),
            device=trajectory.device,
        ).long()
        # Add noise to the clean trajectories according to the noise magnitude at each timestep.
        noisy_trajectory = self.noise_scheduler.add_noise(trajectory, eps, timesteps)

        # Run the denoising network (that might denoise the trajectory, or attempt to predict the noise).
        pred = self.unet(noisy_trajectory, timesteps, global_cond=global_cond)

        # Compute the loss.
        # The target is either the original trajectory, or the noise.
        if self.config.prediction_type == "epsilon":
            target = eps
        elif self.config.prediction_type == "sample":
            target = batch["action"]
        else:
            raise ValueError(f"Unsupported prediction type {self.config.prediction_type}")

        loss = F.mse_loss(pred, target, reduction="none")

        # Mask loss wherever the action is padded with copies (edges of the dataset trajectory).
        if self.config.do_mask_loss_for_padding and "action_is_pad" in batch:
            in_episode_bound = ~batch["action_is_pad"]
            loss = loss * in_episode_bound.unsqueeze(-1)

        return loss.mean()


class OctoRgbEncoder(nn.Module):
    """Encoder an RGB image into a 1D feature vector.

    Includes the ability to normalize and crop the image first.
    """

    def __init__(self, config: OctoConfig):
        super().__init__()
        # Set up optional preprocessing.
        if config.crop_shape is not None:
            self.do_crop = True
            # Always use center crop for eval
            self.center_crop = torchvision.transforms.CenterCrop(config.crop_shape)
            if config.crop_is_random:
                self.maybe_random_crop = torchvision.transforms.RandomCrop(config.crop_shape)
            else:
                self.maybe_random_crop = self.center_crop
        else:
            self.do_crop = False

        # Set up backbone.
        backbone_model = getattr(torchvision.models, config.vision_backbone)(
            weights=config.pretrained_backbone_weights
        )
        # Note: This assumes that the layer4 feature map is children()[-3]
        # TODO(alexander-soare): Use a safer alternative.
        self.backbone = nn.Sequential(*(list(backbone_model.children())[:-2]))
        if config.use_group_norm:
            if config.pretrained_backbone_weights:
                raise ValueError(
                    "You can't replace BatchNorm in a pretrained model without ruining the weights!"
                )
            self.backbone = _replace_submodules(
                root_module=self.backbone,
                predicate=lambda x: isinstance(x, nn.BatchNorm2d),
                func=lambda x: nn.GroupNorm(num_groups=x.num_features // 16, num_channels=x.num_features),
            )

        # Use a dry run to get the feature map shape.
        # The dummy input should take the number of image channels from `config.input_shapes` and it should
        # use the height and width from `config.crop_shape`.
        image_keys = [k for k in config.input_shapes if k.startswith("observation.image")]
        assert len(image_keys) == 1
        image_key = image_keys[0]
        dummy_input = torch.zeros(size=(1, config.input_shapes[image_key][0], *config.crop_shape))
        with torch.inference_mode():
            dummy_feature_map = self.backbone(dummy_input)
        self.feature_map_shape = tuple(dummy_feature_map.shape[1:])

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: (B, C, H, W) image tensor with pixel values in [0, 1].
        Returns:
            (B, D) image feature.
        """
        # Preprocess: maybe crop (if it was set up in the __init__).
        if self.do_crop:
            if self.training:  # noqa: SIM108
                x = self.maybe_random_crop(x)
            else:
                # Always use center crop for eval.
                x = self.center_crop(x)
        # Extract backbone feature.
        return self.backbone(x)


def _replace_submodules(
    root_module: nn.Module, predicate: Callable[[nn.Module], bool], func: Callable[[nn.Module], nn.Module]
) -> nn.Module:
    """
    Args:
        root_module: The module for which the submodules need to be replaced
        predicate: Takes a module as an argument and must return True if the that module is to be replaced.
        func: Takes a module as an argument and returns a new module to replace it with.
    Returns:
        The root module with its submodules replaced.
    """
    if predicate(root_module):
        return func(root_module)

    replace_list = [k.split(".") for k, m in root_module.named_modules(remove_duplicate=True) if predicate(m)]
    for *parents, k in replace_list:
        parent_module = root_module
        if len(parents) > 0:
            parent_module = root_module.get_submodule(".".join(parents))
        if isinstance(parent_module, nn.Sequential):
            src_module = parent_module[int(k)]
        else:
            src_module = getattr(parent_module, k)
        tgt_module = func(src_module)
        if isinstance(parent_module, nn.Sequential):
            parent_module[int(k)] = tgt_module
        else:
            setattr(parent_module, k, tgt_module)
    # verify that all BN are replaced
    assert not any(predicate(m) for _, m in root_module.named_modules(remove_duplicate=True))
    return root_module


class OctoTransformer(nn.Module):
    def __init__(
        self, embed_dim, n_layers, n_heads, d_ffn, causal_mask=None, pos_emb_max_len=1000, dropout=0.1
    ):
        super().__init__()

        self.register_buffer("causal_mask", causal_mask)
        self.pos_emb = PositionalEncoding(pos_emb_max_len, embed_dim, dropout=dropout)
        encoder_layers = TransformerEncoderLayer(
            embed_dim, n_heads, dim_feedforward=d_ffn, dropout=dropout, batch_first=True
        )
        self.transformer_encoder = TransformerEncoder(encoder_layers, n_layers)

    def forward(self, x):
        x = self.pos_emb(x)
        x = self.transformer_encoder(x, mask=self.causal_mask)
        return x


def make_mask(obs_len, n_obs, n_readouts=1):
    # very janky implementation of group-wise masking
    size = (obs_len + n_readouts) * n_obs
    mask = torch.full((size, size), -float("inf"))
    mask = torch.triu(mask, diagonal=1)
    for i in range(n_obs):
        mask[:, ((i + 1) * obs_len) + i] = -float("inf")
        mask[((i + 1) * obs_len) + i, ((i + 1) * obs_len) + i] = 0
    return mask


class OctoNet(nn.Module):
    def __init__(
        self,
        n_obs,
        qpos_dim,
        img_dim,
        embed_dim,
        n_layers,
        n_heads,
        d_ffn,
        obs_seq_len,
        dropout=0.1,
        use_causal_mask=True,
    ):
        super().__init__()

        # hardcode this to use only one readout head for now
        n_readouts = 1

        pos_emb_max_len = obs_seq_len + n_readouts * n_obs
        self.n_readouts = n_readouts

        causal_mask = None
        if use_causal_mask:
            causal_mask = make_mask(obs_seq_len // n_obs, n_obs)
        self.qpos_proj = nn.Linear(qpos_dim, embed_dim)
        self.img_proj = nn.Linear(img_dim, embed_dim)
        self.readout_tokens = nn.Parameter(torch.randn((1, n_obs, n_readouts, embed_dim)))
        self.octo_transformer = OctoTransformer(
            embed_dim,
            n_layers,
            n_heads,
            d_ffn,
            causal_mask=causal_mask,
            dropout=dropout,
            pos_emb_max_len=pos_emb_max_len,
        )

    def forward(self, qpos, img_feats):
        b, t, *_ = img_feats.size()
        img_proj = self.img_proj(img_feats)
        qpos_proj = self.qpos_proj(qpos)
        x = torch.cat(
            [img_proj, qpos_proj.view((b, t, 1, -1)), repeat(self.readout_tokens, "1 t r d -> b t r d", b=b)],
            dim=2,
        )
        x = rearrange(x, "b t l f -> b (t l) f")
        x = self.octo_transformer(x)
        x = rearrange(x, "b (t l) f -> b t l f", t=t)
        readout_embeds = x[:, :, -self.n_readouts :, :]
        return readout_embeds


class DiffusionActionHead(nn.Module):
    def __init__(self, time_dim, cond_dim, actions_dim, n_diffusion_head_layers):
        super().__init__()

        self.ff = FourierFeatures(time_dim)
        self.time_ff_encoder = TimeMLP(time_dim, (2 * time_dim, time_dim))
        self.net = MLPResNet(
            time_dim + cond_dim + actions_dim, actions_dim, hidden_dim=256, num_layers=n_diffusion_head_layers
        )

    def forward(self, readout_embeds, time, actions):
        obs_enc = readout_embeds.mean(dim=(1, 2))
        ff = self.ff(time)
        t_cond = self.time_ff_encoder(ff)
        x = torch.cat([t_cond, obs_enc, actions], dim=-1)
        eps_pred = self.net(x)
        return eps_pred
