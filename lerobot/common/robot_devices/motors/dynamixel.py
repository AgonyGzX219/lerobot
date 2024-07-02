import enum
from copy import deepcopy

import numpy as np
from dynamixel_sdk import (
    COMM_SUCCESS,
    DXL_HIBYTE,
    DXL_HIWORD,
    DXL_LOBYTE,
    DXL_LOWORD,
    GroupSyncRead,
    GroupSyncWrite,
    PacketHandler,
    PortHandler,
)

PROTOCOL_VERSION = 2.0
BAUD_RATE = 1_000_000
TIMEOUT_MS = 1000

# https://emanual.robotis.com/docs/en/dxl/x/xl330-m077
# https://emanual.robotis.com/docs/en/dxl/x/xl330-m288
# https://emanual.robotis.com/docs/en/dxl/x/xl430-w250
# https://emanual.robotis.com/docs/en/dxl/x/xm430-w350
# https://emanual.robotis.com/docs/en/dxl/x/xm540-w270

# data_name: (address, size_byte)
X_SERIES_CONTROL_TABLE = {
    "Model_Number": (0, 2),
    "Model_Information": (2, 4),
    "Firmware_Version": (6, 1),
    "ID": (7, 1),
    "Baud_Rate": (8, 1),
    "Return_Delay_Time": (9, 1),
    "Drive_Mode": (10, 1),
    "Operating_Mode": (11, 1),
    "Secondary_ID": (12, 1),
    "Protocol_Type": (13, 1),
    "Homing_Offset": (20, 4),
    "Moving_Threshold": (24, 4),
    "Temperature_Limit": (31, 1),
    "Max_Voltage_Limit": (32, 2),
    "Min_Voltage_Limit": (34, 2),
    "PWM_Limit": (36, 2),
    "Current_Limit": (38, 2),
    "Acceleration_Limit": (40, 4),
    "Velocity_Limit": (44, 4),
    "Max_Position_Limit": (48, 4),
    "Min_Position_Limit": (52, 4),
    "Shutdown": (63, 1),
    "Torque_Enable": (64, 1),
    "LED": (65, 1),
    "Status_Return_Level": (68, 1),
    "Registered_Instruction": (69, 1),
    "Hardware_Error_Status": (70, 1),
    "Velocity_I_Gain": (76, 2),
    "Velocity_P_Gain": (78, 2),
    "Position_D_Gain": (80, 2),
    "Position_I_Gain": (82, 2),
    "Position_P_Gain": (84, 2),
    "Feedforward_2nd_Gain": (88, 2),
    "Feedforward_1st_Gain": (90, 2),
    "Bus_Watchdog": (98, 1),
    "Goal_PWM": (100, 2),
    "Goal_Current": (102, 2),
    "Goal_Velocity": (104, 4),
    "Profile_Acceleration": (108, 4),
    "Profile_Velocity": (112, 4),
    "Goal_Position": (116, 4),
    "Realtime_Tick": (120, 2),
    "Moving": (122, 1),
    "Moving_Status": (123, 1),
    "Present_PWM": (124, 2),
    "Present_Current": (126, 2),
    "Present_Velocity": (128, 4),
    "Present_Position": (132, 4),
    "Velocity_Trajectory": (136, 4),
    "Position_Trajectory": (140, 4),
    "Present_Input_Voltage": (144, 2),
    "Present_Temperature": (146, 1),
}

CALIBRATION_REQUIRED = ["Goal_Position", "Present_Position"]
CONVERT_UINT32_TO_INT32_REQUIRED = ["Goal_Position", "Present_Position"]
# CONVERT_POSITION_TO_ANGLE_REQUIRED = ["Goal_Position", "Present_Position"]
CONVERT_POSITION_TO_ANGLE_REQUIRED = []

MODEL_CONTROL_TABLE = {
    "x_series": X_SERIES_CONTROL_TABLE,
    "xl330-m077": X_SERIES_CONTROL_TABLE,
    "xl330-m288": X_SERIES_CONTROL_TABLE,
    "xl430-w250": X_SERIES_CONTROL_TABLE,
    "xm430-w350": X_SERIES_CONTROL_TABLE,
    "xm540-w270": X_SERIES_CONTROL_TABLE,
}


def uint32_to_int32(values: np.ndarray):
    """
    Convert an unsigned 32-bit integer array to a signed 32-bit integer array.
    """
    for i in range(len(values)):
        if values[i] is not None and values[i] > 2147483647:
            values[i] = values[i] - 4294967296
    return values


def int32_to_uint32(values: np.ndarray):
    """
    Convert a signed 32-bit integer array to an unsigned 32-bit integer array.
    """
    for i in range(len(values)):
        if values[i] is not None and values[i] < 0:
            values[i] = values[i] + 4294967296
    return values


def motor_position_to_angle(position: np.ndarray) -> np.ndarray:
    """
    Convert from motor position in [-2048, 2048] to radian in [-pi, pi]
    """
    return (position / 2048) * 3.14


def motor_angle_to_position(angle: np.ndarray) -> np.ndarray:
    """
    Convert from radian in [-pi, pi] to motor position in [-2048, 2048]
    """
    return ((angle / 3.14) * 2048).astype(np.int64)


# def pwm2vel(pwm: np.ndarray) -> np.ndarray:
#     """
#     :param pwm: numpy array of pwm/s joint velocities
#     :return: numpy array of rad/s joint velocities
#     """
#     return pwm * 3.14 / 2048


# def vel2pwm(vel: np.ndarray) -> np.ndarray:
#     """
#     :param vel: numpy array of rad/s joint velocities
#     :return: numpy array of pwm/s joint velocities
#     """
#     return (vel * 2048 / 3.14).astype(np.int64)


def get_group_sync_key(data_name, motor_names):
    group_key = f"{data_name}_" + "_".join(motor_names)
    return group_key


class TorqueMode(enum.Enum):
    ENABLED = 1
    DISABLED = 0


class OperatingMode(enum.Enum):
    VELOCITY = 1
    POSITION = 3
    EXTENDED_POSITION = 4
    CURRENT_CONTROLLED_POSITION = 5
    PWM = 16
    UNKNOWN = -1


class DriveMode(enum.Enum):
    NON_INVERTED = 0
    INVERTED = 1


class DynamixelMotorsBus:
    def __init__(
        self,
        port: str,
        motors: dict[str, tuple[int, str]],
        extra_model_control_table: dict[str, list[tuple]] | None = None,
    ):
        self.port = port
        self.motors = motors

        self.model_ctrl_table = deepcopy(MODEL_CONTROL_TABLE)
        if extra_model_control_table:
            self.model_ctrl_table.update(extra_model_control_table)

        self.port_handler = PortHandler(self.port)
        self.packet_handler = PacketHandler(PROTOCOL_VERSION)

        if not self.port_handler.openPort():
            raise OSError(f"Failed to open port {self.port}")

        self.port_handler.setBaudRate(BAUD_RATE)
        self.port_handler.setPacketTimeoutMillis(TIMEOUT_MS)

        self.group_readers = {}
        self.group_writers = {}

        self.calibration = None

    @property
    def motor_names(self) -> list[int]:
        return list(self.motors.keys())

    def set_calibration(self, calibration: dict[str, tuple[int, bool]]):
        self.calibration = calibration

    def apply_calibration(self, values: np.ndarray | list, motor_names: list[str] | None):
        if not self.calibration:
            return values

        if motor_names is None:
            motor_names = self.motor_names

        for i, name in enumerate(motor_names):
            homing_offset, drive_mode = self.calibration[name]

            if values[i] is not None:
                if drive_mode:
                    values[i] *= -1
                values[i] += homing_offset

        return values

    def revert_calibration(self, values: np.ndarray | list, motor_names: list[str] | None):
        if not self.calibration:
            return values

        if motor_names is None:
            motor_names = self.motor_names

        for i, name in enumerate(motor_names):
            homing_offset, drive_mode = self.calibration[name]

            if values[i] is not None:
                values[i] -= homing_offset
                if drive_mode:
                    values[i] *= -1

        return values

    def read(self, data_name, motor_names: list[str] | None = None):
        if motor_names is None:
            motor_names = self.motor_names

        motor_ids = []
        models = []
        for name in motor_names:
            motor_idx, model = self.motors[name]
            motor_ids.append(motor_idx)
            models.append(model)

        # TODO(rcadene): assert all motors follow same address
        addr, bytes = self.model_ctrl_table[model][data_name]
        group_key = get_group_sync_key(data_name, motor_names)

        if data_name not in self.group_readers:
            # create new group reader
            self.group_readers[group_key] = GroupSyncRead(self.port_handler, self.packet_handler, addr, bytes)
            for idx in motor_ids:
                self.group_readers[group_key].addParam(idx)

        comm = self.group_readers[group_key].txRxPacket()
        if comm != COMM_SUCCESS:
            raise ConnectionError(
                f"Read failed due to communication error on port {self.port} for group_key {group_key}: "
                f"{self.packet_handler.getTxRxResult(comm)}"
            )

        values = []
        for idx in motor_ids:
            value = self.group_readers[group_key].getData(idx, addr, bytes)
            values.append(value)

        values = np.array(values)

        # TODO(rcadene): explain why
        if data_name in CONVERT_UINT32_TO_INT32_REQUIRED:
            values = uint32_to_int32(values)

        if data_name in CALIBRATION_REQUIRED:
            values = self.apply_calibration(values, motor_names)

        if data_name in CONVERT_POSITION_TO_ANGLE_REQUIRED:
            values = motor_position_to_angle(values)

        return values

    def write(self, data_name, values: int | float | np.ndarray, motor_names: str | list[str] | None = None):
        if motor_names is None:
            motor_names = self.motor_names

        if isinstance(motor_names, str):
            motor_names = [motor_names]

        motor_ids = []
        models = []
        for name in motor_names:
            motor_idx, model = self.motors[name]
            motor_ids.append(motor_idx)
            models.append(model)

        if isinstance(values, (int, float, np.integer)):
            values = [int(values)] * len(motor_ids)

        values = np.array(values)

        if data_name in CONVERT_POSITION_TO_ANGLE_REQUIRED:
            values = motor_angle_to_position(values)

        if data_name in CALIBRATION_REQUIRED:
            values = self.revert_calibration(values, motor_names)

        # TODO(rcadene): why dont we do it?
        # if data_name in CONVERT_INT32_TO_UINT32_REQUIRED:
        #     values = int32_to_uint32(values)

        values = values.tolist()

        # TODO(rcadene): assert all motors follow same address
        addr, bytes = self.model_ctrl_table[model][data_name]
        group_key = get_group_sync_key(data_name, motor_names)

        init_group = data_name not in self.group_readers
        if init_group:
            self.group_writers[group_key] = GroupSyncWrite(
                self.port_handler, self.packet_handler, addr, bytes
            )

        for idx, value in zip(motor_ids, values, strict=False):
            if bytes == 1:
                data = [
                    DXL_LOBYTE(DXL_LOWORD(value)),
                ]
            elif bytes == 2:
                data = [
                    DXL_LOBYTE(DXL_LOWORD(value)),
                    DXL_HIBYTE(DXL_LOWORD(value)),
                ]
            elif bytes == 4:
                data = [
                    DXL_LOBYTE(DXL_LOWORD(value)),
                    DXL_HIBYTE(DXL_LOWORD(value)),
                    DXL_LOBYTE(DXL_HIWORD(value)),
                    DXL_HIBYTE(DXL_HIWORD(value)),
                ]
            else:
                raise NotImplementedError(
                    f"Value of the number of bytes to be sent is expected to be in [1, 2, 4], but "
                    f"{bytes} is provided instead."
                )

            if init_group:
                self.group_writers[group_key].addParam(idx, data)
            else:
                self.group_writers[group_key].changeParam(idx, data)

        comm = self.group_writers[group_key].txPacket()
        if comm != COMM_SUCCESS:
            raise ConnectionError(
                f"Write failed due to communication error on port {self.port} for group_key {group_key}: "
                f"{self.packet_handler.getTxRxResult(comm)}"
            )

    # def read(self, data_name, motor_name: str):
    #     motor_idx, model = self.motors[motor_name]
    #     addr, bytes = self.model_ctrl_table[model][data_name]

    #     args = (self.port_handler, motor_idx, addr)
    #     if bytes == 1:
    #         value, comm, err = self.packet_handler.read1ByteTxRx(*args)
    #     elif bytes == 2:
    #         value, comm, err = self.packet_handler.read2ByteTxRx(*args)
    #     elif bytes == 4:
    #         value, comm, err = self.packet_handler.read4ByteTxRx(*args)
    #     else:
    #         raise NotImplementedError(
    #             f"Value of the number of bytes to be sent is expected to be in [1, 2, 4], but "
    #             f"{bytes} is provided instead.")

    #     if comm != COMM_SUCCESS:
    #         raise ConnectionError(
    #             f"Read failed due to communication error on port {self.port} for motor {motor_idx}: "
    #             f"{self.packet_handler.getTxRxResult(comm)}"
    #         )
    #     elif err != 0:
    #         raise ConnectionError(
    #             f"Read failed due to error {err} on port {self.port} for motor {motor_idx}: "
    #             f"{self.packet_handler.getTxRxResult(err)}"
    #         )

    #     if data_name in CALIBRATION_REQUIRED:
    #         value = self.apply_calibration([value], [motor_name])[0]

    #     return value

    # def write(self, data_name, value, motor_name: str):
    #     if data_name in CALIBRATION_REQUIRED:
    #         value = self.revert_calibration([value], [motor_name])[0]

    #     motor_idx, model = self.motors[motor_name]
    #     addr, bytes = self.model_ctrl_table[model][data_name]
    #     args = (self.port_handler, motor_idx, addr, value)
    #     if bytes == 1:
    #         comm, err = self.packet_handler.write1ByteTxRx(*args)
    #     elif bytes == 2:
    #         comm, err = self.packet_handler.write2ByteTxRx(*args)
    #     elif bytes == 4:
    #         comm, err = self.packet_handler.write4ByteTxRx(*args)
    #     else:
    #         raise NotImplementedError(
    #             f"Value of the number of bytes to be sent is expected to be in [1, 2, 4], but {bytes} "
    #             f"is provided instead.")

    #     if comm != COMM_SUCCESS:
    #         raise ConnectionError(
    #             f"Write failed due to communication error on port {self.port} for motor {motor_idx}: "
    #             f"{self.packet_handler.getTxRxResult(comm)}"
    #         )
    #     elif err != 0:
    #         raise ConnectionError(
    #             f"Write failed due to error {err} on port {self.port} for motor {motor_idx}: "
    #             f"{self.packet_handler.getTxRxResult(err)}"
    #         )
