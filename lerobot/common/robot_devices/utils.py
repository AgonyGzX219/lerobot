import platform
import time


def busy_wait(seconds):
    if seconds <= 0:
        return

    if platform.system() == "Darwin":
        # On Mac, `time.sleep` is not accurate and we need to use this while loop trick,
        # but it consumes CPU cycles.
        # TODO(rcadene): find an alternative: from python 11, time.sleep is precise

        start_sleep = time.perf_counter()
        time.sleep(seconds / 2)
        dt_sleep = time.perf_counter() - start_sleep

        end_time = time.perf_counter() + (seconds - dt_sleep)
        while time.perf_counter() < end_time:
            pass
    else:
        # On Linux time.sleep is accurate
        time.sleep(seconds)


class RobotDeviceNotConnectedError(Exception):
    """Exception raised when the robot device is not connected."""

    def __init__(
        self, message="This robot device is not connected. Try calling `robot_device.connect()` first."
    ):
        self.message = message
        super().__init__(self.message)


class RobotDeviceAlreadyConnectedError(Exception):
    """Exception raised when the robot device is already connected."""

    def __init__(
        self,
        message="This robot device is already connected. Try not calling `robot_device.connect()` twice.",
    ):
        self.message = message
        super().__init__(self.message)
