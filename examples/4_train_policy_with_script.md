This tutorial will explain the training script, how to use it, and particularly the use of Hydra to configure everything needed for the training run.

## The training script

LeRobot offers a training script at [`lerobot/scripts/train.py`](../../lerobot/scripts/train.py). At a high level it does the following:

- Loads a Hydra configuration file for the following steps (more on Hydra in a moment).
- Makes a simulation environment.
- Makes a dataset corresponding to that simulation environment.
- Makes a policy.
- Runs a standard training loop with forward pass, backward pass, optimization step, and occasional logging, evaluation (of the policy on the environment), and checkpointing.

## Our use of Hydra

Explaining the ins and outs of [Hydra](https://hydra.cc/docs/intro/) is beyond the scope of this document, but here we'll share the main points you need to know.

First, consider that `lerobot/configs` might have a directory structure like this (this is the case at the time of writing):

```
.
├── default.yaml
├── env
│   ├── aloha.yaml
│   ├── pusht.yaml
│   └── xarm.yaml
└── policy
    ├── act.yaml
    ├── diffusion.yaml
    └── tdmpc.yaml
```

**_For brevity, in the rest of this document we'll drop the leading `lerobot/configs` path. So `default.yaml` really refers to `lerobot/configs/default.yaml`._**

When you run the training script, Hydra takes over via the `@hydra.main` decorator. If you take a look at the `@hydra.main`'s arguments you will see `config_path="../configs", config_name="default"`. This means Hydra looks for `default.yaml` in `../configs` (which resolves to `lerobot/configs`).

Among regular configuration hyperparameters like `device: cuda`, `default.yaml` has a `defaults` section. It might look like this.

```yaml
defaults:
  - _self_
  - env: pusht
  - policy: diffusion
```

So, Hydra will grab `env/pusht.yaml` and `policy/diffusion.yaml` and incorporate their configuration parameters (any configuration parameters already present in `default.yaml` are overriden).

## Running the training script with our provided configurations

If you want to train Diffusion Policy with PushT, you really only need to run:

```bash
python lerobot/scripts/train.py
```

That's because `default.yaml` already defaults to using Diffusion Policy and PushT. To be more explicit, you could also do the following (which would have the same effect):

```bash
python lerobot/scripts/train.py policy=diffusion env=pusht
```

If you want to train ACT with Aloha, you can do:

```bash
python lerobot/scripts/train.py policy=act env=aloha
```

**Notice, how the config overrides are passed** as `param_name=param_value`. This is the format the Hydra excepts for parsing the overrides.

## Overriding configuration parameters in the CLI

If you look in `env/aloha.yaml` you might see:

```yaml
# lerobot/configs/env/aloha.yaml
env:
  task: AlohaInsertion-v0
```

And if you look in `policy/act.yaml` you might see:

```yaml
# lerobot/configs/policy/act.yaml
dataset_repo_id: lerobot/aloha_sim_insertion_human
```

But our Aloha environment actually supports a cube transfer task as well. To train for this task, you _could_ modify the two configuration files respectively.

We need to select the cube transfer task for the ALOHA environment.

```yaml
# lerobot/configs/env/aloha.yaml
env:
   task: AlohaTransferCube-v0
```

We also need to use the cube transfer dataset.

```yaml
# lerobot/configs/policy/act.yaml
dataset_repo_id: lerobot/aloha_sim_transfer_cube_human
```

Now you'd be able to run:

```bash
python lerobot/scripts/train.py policy=act env=aloha
```

and you'd be training and evaluating on the cube transfer task.

OR, your could leave the configuration files in their original state and override the defaults via the command line:

```bash
python lerobot/scripts/train.py \
    policy=act \
    dataset_repo_id=lerobot/aloha_sim_transfer_cube_human \
    env=aloha \
    env.task=AlohaTransferCube-v0
```

There's something new here. Notice the `.` delimiter used to traverse the configuration hierarchy.

Putting all that knowledge together, here's the command that was used to train https://huggingface.co/lerobot/act_aloha_sim_transfer_cube_human.

```bash
python lerobot/scripts/train.py \
    hydra.run.dir=outputs/train/act_aloha_sim_transfer_cube_human \
    device=cuda
    env=aloha \
    env.task=AlohaTransferCube-v0 \
    dataset_repo_id=lerobot/aloha_sim_transfer_cube_human \
    policy=act \
    training.eval_freq=10000 \
    training.log_freq=250 \
    training.offline_steps=100000 \
    training.save_model=true \
    training.save_freq=25000 \
    eval.n_episodes=50 \
    eval.batch_size=50 \
    wandb.enable=false \
```

There's one new thing here: `hydra.run.dir=outputs/train/act_aloha_sim_transfer_cube_human`, which specifies where to save the training output.

---

Now, why don't you try running:

```bash
python lerobot/scripts/train.py policy=act env=pusht dataset_repo_id=lerobot/pusht
```

That was a little mean of us, because if you did try running that code, you almost certainly got an exception of sorts. That's because there are aspects of the ACT configuration that are specific to the ALOHA environments, and here we have tried to use PushT.

Please, head on over to our advanced [tutorial on adapting policy configuration to various environments](./advanced/train_act_pusht/train_act_pusht.md).

Or in the meantime, happy coding! 🤗
