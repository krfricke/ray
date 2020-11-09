from enum import Enum
from typing import Any, Callable, Union


class Verbosity(Enum):
    V0_MINIMAL = 0
    V1_EXPERIMENT = 1
    V2_TRIAL_NORM = 2
    V3_TRIAL_DETAILS = 3

    def __int__(self):
        return self.value


verbosity: Union[int, Verbosity] = Verbosity.V3_TRIAL_DETAILS


def set_verbosity(level: Union[int, Verbosity]):
    global verbosity

    if isinstance(level, int):
        verbosity = Verbosity(level)
    else:
        verbosity = verbosity


def verbose_log(logger: Callable[[str], Any], level: Union[int, Verbosity],
                message: str):
    """Log `message` if specified level exceeds global verbosity level.

    `logger` should be a Callable, e.g. `logger.info`. It can also be
    `print` or a logger method of any other level - or any callable that
    accepts a string.
    """
    if has_verbosity(level):
        logger(message)


def has_verbosity(level: Union[int, Verbosity]) -> bool:
    """Return True if passed level exceeds global verbosity level."""
    global verbosity

    log_level = int(level)
    verbosity_level = int(verbosity)

    return verbosity_level >= log_level
