import pickle
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import torch

from lerobot.common.robot_devices.cameras.utils import Camera
from lerobot.common.robot_devices.motors.dynamixel import (
    OperatingMode,
    TorqueMode,
)
from lerobot.common.robot_devices.motors.utils import MotorsBus
from lerobot.common.robot_devices.utils import RobotDeviceAlreadyConnectedError, RobotDeviceNotConnectedError

########################################################################
# Calibration logic
########################################################################

AVAILABLE_ROBOT_TYPES = ["koch", "aloha"]

URL_TEMPLATE = (
    "https://raw.githubusercontent.com/huggingface/lerobot/main/media/{robot}/{arm}_{position}.webp"
)

# In nominal range ]-2048, 2048[
# First target position consists in moving koch arm to a straight horizontal position with gripper closed.
KOCH_FIRST_POSITION = np.array([0, 0, 0, 0, 0, 0], dtype=np.int32)
# Second target position consists in moving koch arm from the first target position by rotating every motor
# by 90 degree. When the direction is ambiguous, always rotate on the right. Gripper is open, directed towards you.
# TODO(rcadene): Take motor resolution into account instead of assuming 4096
KOCH_SECOND_POSITION = np.array([1024, 1024, 1024, 1024, 1024, 1024], dtype=np.int32)

# In nominal range ]-180, 180[
KOCH_GRIPPER_OPEN = 35.156
KOCH_REST_POSITION = np.array([0, 135, 90, 0, 0, KOCH_GRIPPER_OPEN])

# In nominal range ]-2048, 2048[
ALOHA_FIRST_POSITION = np.array([0, 0, 0, 0, 0, 0, 0, 0, 0], dtype=np.int32)
ALOHA_SECOND_POSITION = np.array([1024, 1024, 1024, 1024, 1024, 1024, 1024, 1024, 512], dtype=np.int32)
# In nominal range ]-180, 180[
ALOHA_GRIPPER_OPEN = 30
ALOHA_REST_POSITION = np.array([0, 0, 0, 0, 0, 0, 0, 0, 0], dtype=np.int32)


def assert_robot_type(robot_type):
    if robot_type not in AVAILABLE_ROBOT_TYPES:
        raise ValueError(robot_type)


def get_first_position(robot_type):
    if robot_type == "koch":
        return KOCH_FIRST_POSITION
    elif robot_type == "aloha":
        return ALOHA_FIRST_POSITION


def get_second_position(robot_type):
    if robot_type == "koch":
        return KOCH_SECOND_POSITION
    elif robot_type == "aloha":
        return ALOHA_SECOND_POSITION


def get_rest_position(robot_type):
    if robot_type == "koch":
        return KOCH_REST_POSITION
    elif robot_type == "aloha":
        return ALOHA_REST_POSITION


def assert_drive_mode(drive_mode):
    # `drive_mode` is in [0,1] with 0 means original rotation direction for the motor, and 1 means inverted.
    if not np.all(np.isin(drive_mode, [0, 1])):
        raise ValueError(f"`drive_mode` contains values other than 0 or 1: ({drive_mode})")


def apply_drive_mode(position, drive_mode):
    assert_drive_mode(drive_mode)
    # Convert `drive_mode` from [0, 1] with 0 indicates original rotation direction and 1 inverted,
    # to [-1, 1] with 1 indicates original rotation direction and -1 inverted.
    signed_drive_mode = -(drive_mode * 2 - 1)
    position *= signed_drive_mode
    return position


def compute_nearest_rounded_position(position, second_position):
    # TODO(rcadene): Take motor resolution into account instead of assuming 4096
    # Assumes 4096 steps for a full revolution for the motors
    # Hence 90 degree is 1024 steps
    return np.round(position / second_position).astype(position.dtype) * second_position


def reset_arm(arm: MotorsBus):
    # To be configured, all servos must be in "torque disable" mode
    arm.write("Torque_Enable", TorqueMode.DISABLED.value)

    # Use 'extended position mode' for all motors except gripper, because in joint mode the servos can't
    # rotate more than 360 degrees (from 0 to 4095) And some mistake can happen while assembling the arm,
    # you could end up with a servo with a position 0 or 4095 at a crucial point See [
    # https://emanual.robotis.com/docs/en/dxl/x/x_series/#operating-mode11]
    all_motors_except_gripper = [name for name in arm.motor_names if name != "gripper"]
    if len(all_motors_except_gripper) > 0:
        arm.write("Operating_Mode", OperatingMode.EXTENDED_POSITION.value, all_motors_except_gripper)

    # TODO(rcadene): why?
    # Use 'position control current based' for gripper
    arm.write("Operating_Mode", OperatingMode.CURRENT_CONTROLLED_POSITION.value, "gripper")


def run_arm_calibration(arm: MotorsBus, robot_type: str, arm_name: str, arm_type: str):
    """Example of usage:
    ```python
    run_arm_calibration(arm, "aloha", "left", "follower")
    ```
    """
    reset_arm(arm)

    print(f"\nRunning calibration of {robot_type} {arm_name} {arm_type}...")

    # TODO(rcadene): document what position 1 mean
    print("\nMove arm to first target position")
    print("See: " + URL_TEMPLATE.format(robot=robot_type, arm=arm_type, position="first"))
    input("Press Enter to continue...")

    # The first position zeros all motors, i.e. after calibration, if write goal position to be all 0,
    # the robot will move to this first position.
    first_position = get_first_position(robot_type)
    # The second position rotates all motors with 90 degrees angle clock-wise from the perspective of the first motor or the preceeding motor in the chain.
    # Note: if 90 degree rotation cannot be achieved (e.g. gripper of Aloha), then it will rotate to 45 degrees.
    second_position = get_second_position(robot_type)

    # Compute homing offset so that `present_position + homing_offset ~= target_position`
    position = arm.read("Present_Position")
    position = compute_nearest_rounded_position(position, second_position)
    homing_offset = first_position - position

    # TODO(rcadene): document what position 2 mean
    print("\nMove arm to second target position")
    print("See: " + URL_TEMPLATE.format(robot=robot_type, arm=arm_type, position="second"))
    input("Press Enter to continue...")

    # Find drive mode by rotating each motor by 90 degree.
    # After applying homing offset, if position equals target position, then drive mode is 0,
    # to indicate an original rotation direction for the motor ; else, drive mode is 1,
    # to indicate an inverted rotation direction.
    position = arm.read("Present_Position")
    position += homing_offset
    position = compute_nearest_rounded_position(position, second_position)
    drive_mode = (position != second_position).astype(np.int32)

    # Re-compute homing offset to take into account drive mode
    position = arm.read("Present_Position")
    position = apply_drive_mode(position, drive_mode)
    position = compute_nearest_rounded_position(position, second_position)
    homing_offset = second_position - position

    print("\nMove arm to rest position")
    print("See: " + URL_TEMPLATE.format(robot=robot_type, arm=arm_type, position="rest"))
    input("Press Enter to continue...")
    print()

    return homing_offset, drive_mode


########################################################################
# Alexander Koch robot arm
########################################################################


@dataclass
class KochRobotConfig:
    """
    Example of usage:
    ```python
    KochRobotConfig()
    ```
    """

    robot_type: str = "koch"
    # Define all components of the robot
    leader_arms: dict[str, MotorsBus] = field(default_factory=lambda: {})
    follower_arms: dict[str, MotorsBus] = field(default_factory=lambda: {})
    cameras: dict[str, Camera] = field(default_factory=lambda: {})

    def __post_init__(self):
        assert_robot_type(self.robot_type)


class KochRobot:
    # TODO(rcadene): Implement force feedback
    """Tau Robotics: https://tau-robotics.com

    Example of highest frequency teleoperation without camera:
    ```python
    # Defines how to communicate with the motors of the leader and follower arms
    leader_arms = {
        "main": DynamixelMotorsBus(
            port="/dev/tty.usbmodem575E0031751",
            motors={
                # name: (index, model)
                "shoulder_pan": (1, "xl330-m077"),
                "shoulder_lift": (2, "xl330-m077"),
                "elbow_flex": (3, "xl330-m077"),
                "wrist_flex": (4, "xl330-m077"),
                "wrist_roll": (5, "xl330-m077"),
                "gripper": (6, "xl330-m077"),
            },
        ),
    }
    follower_arms = {
        "main": DynamixelMotorsBus(
            port="/dev/tty.usbmodem575E0032081",
            motors={
                # name: (index, model)
                "shoulder_pan": (1, "xl430-w250"),
                "shoulder_lift": (2, "xl430-w250"),
                "elbow_flex": (3, "xl330-m288"),
                "wrist_flex": (4, "xl330-m288"),
                "wrist_roll": (5, "xl330-m288"),
                "gripper": (6, "xl330-m288"),
            },
        ),
    }
    robot = KochRobot(leader_arms, follower_arms)

    # Connect motors buses and cameras if any (Required)
    robot.connect()

    while True:
        robot.teleop_step()
    ```

    Example of highest frequency data collection without camera:
    ```python
    # Assumes leader and follower arms have been instantiated already (see first example)
    robot = KochRobot(leader_arms, follower_arms)
    robot.connect()
    while True:
        observation, action = robot.teleop_step(record_data=True)
    ```

    Example of highest frequency data collection with cameras:
    ```python
    # Defines how to communicate with 2 cameras connected to the computer.
    # Here, the webcam of the laptop and the phone (connected in USB to the laptop)
    # can be reached respectively using the camera indices 0 and 1. These indices can be
    # arbitrary. See the documentation of `OpenCVCamera` to find your own camera indices.
    cameras = {
        "laptop": OpenCVCamera(camera_index=0, fps=30, width=640, height=480),
        "phone": OpenCVCamera(camera_index=1, fps=30, width=640, height=480),
    }

    # Assumes leader and follower arms have been instantiated already (see first example)
    robot = KochRobot(leader_arms, follower_arms, cameras)
    robot.connect()
    while True:
        observation, action = robot.teleop_step(record_data=True)
    ```

    Example of controlling the robot with a policy (without running multiple policies in parallel to ensure highest frequency):
    ```python
    # Assumes leader and follower arms + cameras have been instantiated already (see previous example)
    robot = KochRobot(leader_arms, follower_arms, cameras)
    robot.connect()
    while True:
        # Uses the follower arms and cameras to capture an observation
        observation = robot.capture_observation()

        # Assumes a policy has been instantiated
        with torch.inference_mode():
            action = policy.select_action(observation)

        # Orders the robot to move
        robot.send_action(action)
    ```

    Example of disconnecting which is not mandatory since we disconnect when the object is deleted:
    ```python
    robot.disconnect()
    ```
    """

    def __init__(
        self,
        config: KochRobotConfig | None = None,
        calibration_path: Path = ".cache/calibration/koch.pkl",
        **kwargs,
    ):
        if config is None:
            config = KochRobotConfig()
        # Overwrite config arguments using kwargs
        self.config = replace(config, **kwargs)
        self.calibration_path = Path(calibration_path)

        self.robot_type = self.config.robot_type
        self.leader_arms = self.config.leader_arms
        self.follower_arms = self.config.follower_arms
        self.cameras = self.config.cameras
        self.is_connected = False
        self.logs = {}

    def connect(self):
        if self.is_connected:
            raise RobotDeviceAlreadyConnectedError(
                "KochRobot is already connected. Do not run `robot.connect()` twice."
            )

        if not self.leader_arms and not self.follower_arms and not self.cameras:
            raise ValueError(
                "KochRobot doesn't have any device to connect. See example of usage in docstring of the class."
            )

        # Connect the arms
        for name in self.follower_arms:
            print(f"Connecting {name} follower arm.")
            self.follower_arms[name].connect()
            print(f"Connecting {name} leader arm.")
            self.leader_arms[name].connect()

        # Reset the arms and load or run calibration
        if self.calibration_path.exists():
            # Reset all arms before setting calibration
            for name in self.follower_arms:
                reset_arm(self.follower_arms[name])
            for name in self.leader_arms:
                reset_arm(self.leader_arms[name])

            with open(self.calibration_path, "rb") as f:
                calibration = pickle.load(f)
        else:
            # Run calibration process which begins by reseting all arms
            calibration = self.run_calibration()

            self.calibration_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.calibration_path, "wb") as f:
                pickle.dump(calibration, f)

        # Set calibration
        for name in self.follower_arms:
            self.follower_arms[name].set_calibration(calibration[f"follower_{name}"])
        for name in self.leader_arms:
            self.leader_arms[name].set_calibration(calibration[f"leader_{name}"])

        # TODO(rcadene): before merging, figure out why for Aloha, values are outside 180 degrees range on rest position
        # for name in self.leader_arms:
        #     values = self.leader_arms[name].read("Present_Position")
        #     if (values < -180).any() or (values >= 180).any():
        #         raise ValueError(
        #             f"At least one of the motor of the {name} leader arm has a joint value outside of its centered degree range of ]-180, 180[."
        #             'This "jump of range" can be caused by a hardware issue, or you might have unexpectedly completed a full rotation of the motor '
        #             "during manipulation or transportation of your robot. "
        #             f"The values and motors: {values} {self.leader_arms[name].motor_names}.\n"
        #             "Rotate the arm to fit the range ]-180, 180[ and relaunch the script, or recalibrate all motors by setting a different "
        #             "calibration path during the instatiation of your robot (e.g. `--robot-overrides calibration_path=.cache/calibration/koch_v2.pkl`)"
        #         )

        # Enable torque on all motors of the follower arms
        for name in self.follower_arms:
            self.follower_arms[name].write("Torque_Enable", 1)

        # Custom setup for each robot type
        if self.robot_type == "koch":
            # Enable torque on the gripper of the leader arms, and move it to 45 degrees,
            # so that we can use it as a trigger to close the gripper of the follower arms.
            for name in self.leader_arms:
                self.leader_arms[name].write("Torque_Enable", 1, "gripper")
                self.leader_arms[name].write("Goal_Position", KOCH_GRIPPER_OPEN, "gripper")

            # Set better PID values to close the gap between recorded states and actions
            # TODO(rcadene): Implement an automatic procedure to set optimial PID values for each motor
            for name in self.follower_arms:
                self.follower_arms[name].write("Position_P_Gain", 1500, "elbow_flex")
                self.follower_arms[name].write("Position_I_Gain", 0, "elbow_flex")
                self.follower_arms[name].write("Position_D_Gain", 600, "elbow_flex")

        # Connect the cameras
        for name in self.cameras:
            self.cameras[name].connect()

        self.is_connected = True

    def run_calibration(self):
        calibration = {}

        for name in self.follower_arms:
            homing_offset, drive_mode = run_arm_calibration(
                self.follower_arms[name], self.robot_type, name, "follower"
            )

            calibration[f"follower_{name}"] = {}
            for idx, motor_name in enumerate(self.follower_arms[name].motor_names):
                calibration[f"follower_{name}"][motor_name] = (homing_offset[idx], drive_mode[idx])

        for name in self.leader_arms:
            homing_offset, drive_mode = run_arm_calibration(
                self.leader_arms[name], self.robot_type, name, "leader"
            )

            calibration[f"leader_{name}"] = {}
            for idx, motor_name in enumerate(self.leader_arms[name].motor_names):
                calibration[f"leader_{name}"][motor_name] = (homing_offset[idx], drive_mode[idx])

        return calibration

    def teleop_step(
        self, record_data=False
    ) -> None | tuple[dict[str, torch.Tensor], dict[str, torch.Tensor]]:
        if not self.is_connected:
            raise RobotDeviceNotConnectedError(
                "KochRobot is not connected. You need to run `robot.connect()`."
            )

        # Prepare to assign the position of the leader to the follower
        leader_pos = {}
        for name in self.leader_arms:
            now = time.perf_counter()
            leader_pos[name] = self.leader_arms[name].read("Present_Position")
            self.logs[f"read_leader_{name}_pos_dt_s"] = time.perf_counter() - now

        follower_goal_pos = {}
        for name in self.leader_arms:
            follower_goal_pos[name] = leader_pos[name]

        # Send action
        for name in self.follower_arms:
            now = time.perf_counter()
            self.follower_arms[name].write("Goal_Position", follower_goal_pos[name])
            self.logs[f"write_follower_{name}_goal_pos_dt_s"] = time.perf_counter() - now

        # Early exit when recording data is not requested
        if not record_data:
            return

        # TODO(rcadene): Add velocity and other info
        # Read follower position
        follower_pos = {}
        for name in self.follower_arms:
            now = time.perf_counter()
            follower_pos[name] = self.follower_arms[name].read("Present_Position")
            self.logs[f"read_follower_{name}_pos_dt_s"] = time.perf_counter() - now

        # Create state by concatenating follower current position
        state = []
        for name in self.follower_arms:
            if name in follower_pos:
                state.append(follower_pos[name])
        state = np.concatenate(state)

        # Create action by concatenating follower goal position
        action = []
        for name in self.follower_arms:
            if name in follower_goal_pos:
                action.append(follower_goal_pos[name])
        action = np.concatenate(action)

        # Capture images from cameras
        images = {}
        for name in self.cameras:
            now = time.perf_counter()
            images[name] = self.cameras[name].async_read()
            self.logs[f"read_camera_{name}_dt_s"] = self.cameras[name].logs["delta_timestamp_s"]
            self.logs[f"async_read_camera_{name}_dt_s"] = time.perf_counter() - now

        # Populate output dictionnaries and format to pytorch
        obs_dict, action_dict = {}, {}
        obs_dict["observation.state"] = torch.from_numpy(state)
        action_dict["action"] = torch.from_numpy(action)
        for name in self.cameras:
            obs_dict[f"observation.images.{name}"] = torch.from_numpy(images[name])

        return obs_dict, action_dict

    def capture_observation(self):
        """The returned observations do not have a batch dimension."""
        if not self.is_connected:
            raise RobotDeviceNotConnectedError(
                "KochRobot is not connected. You need to run `robot.connect()`."
            )

        # Read follower position
        follower_pos = {}
        for name in self.follower_arms:
            now = time.perf_counter()
            follower_pos[name] = self.follower_arms[name].read("Present_Position")
            self.logs[f"read_follower_{name}_pos_dt_s"] = time.perf_counter() - now

        # Create state by concatenating follower current position
        state = []
        for name in self.follower_arms:
            if name in follower_pos:
                state.append(follower_pos[name])
        state = np.concatenate(state)

        # Capture images from cameras
        images = {}
        for name in self.cameras:
            now = time.perf_counter()
            images[name] = self.cameras[name].async_read()
            self.logs[f"read_camera_{name}_dt_s"] = self.cameras[name].logs["delta_timestamp_s"]
            self.logs[f"async_read_camera_{name}_dt_s"] = time.perf_counter() - now

        # Populate output dictionnaries and format to pytorch
        obs_dict = {}
        obs_dict["observation.state"] = torch.from_numpy(state)
        for name in self.cameras:
            obs_dict[f"observation.images.{name}"] = torch.from_numpy(images[name])
        return obs_dict

    def send_action(self, action: torch.Tensor):
        """The provided action is expected to be a vector."""
        if not self.is_connected:
            raise RobotDeviceNotConnectedError(
                "KochRobot is not connected. You need to run `robot.connect()`."
            )

        from_idx = 0
        to_idx = 0
        follower_goal_pos = {}
        for name in self.follower_arms:
            if name in self.follower_arms:
                to_idx += len(self.follower_arms[name].motor_names)
                follower_goal_pos[name] = action[from_idx:to_idx].numpy()
                from_idx = to_idx

        for name in self.follower_arms:
            self.follower_arms[name].write("Goal_Position", follower_goal_pos[name].astype(np.int32))

    def disconnect(self):
        if not self.is_connected:
            raise RobotDeviceNotConnectedError(
                "KochRobot is not connected. You need to run `robot.connect()` before disconnecting."
            )

        for name in self.follower_arms:
            self.follower_arms[name].disconnect()

        for name in self.leader_arms:
            self.leader_arms[name].disconnect()

        for name in self.cameras:
            self.cameras[name].disconnect()

        self.is_connected = False

    def __del__(self):
        if getattr(self, "is_connected", False):
            self.disconnect()
