from pathlib import Path

from lerobot.common.policies.factory import make_policy
from lerobot.common.robot_devices.robots.factory import make_robot
from lerobot.common.utils.utils import init_hydra_config
from lerobot.scripts.control_robot import record, replay, teleoperate
from tests.utils import DEFAULT_CONFIG_PATH, DEVICE, KOCH_ROBOT_CONFIG_PATH, require_koch


def make_robot_():
    robot_cfg = init_hydra_config(KOCH_ROBOT_CONFIG_PATH)
    robot = make_robot(robot_cfg)
    return robot


@require_koch
def test_teleoperate(request):
    robot = make_robot_()
    teleoperate(robot, teleop_time_s=1)
    teleoperate(robot, fps=30, teleop_time_s=1)
    teleoperate(robot, fps=60, teleop_time_s=1)
    del robot


@require_koch
def test_record_and_replay_and_policy(tmpdir, request):
    env_name = "koch_real"
    policy_name = "act_koch_real"

    root = Path(tmpdir)
    repo_id = "lerobot/debug"

    robot = make_robot_()
    dataset = record(
        robot, fps=30, root=root, repo_id=repo_id, warmup_time_s=1, episode_time_s=1, num_episodes=2
    )

    replay(robot, episode=0, fps=30, root=root, repo_id=repo_id)

    cfg = init_hydra_config(
        DEFAULT_CONFIG_PATH,
        overrides=[
            f"env={env_name}",
            f"policy={policy_name}",
            f"device={DEVICE}",
        ],
    )

    policy = make_policy(hydra_cfg=cfg, dataset_stats=dataset.stats)

    record(robot, policy, cfg, run_time_s=1)

    del robot
