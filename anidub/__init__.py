import logging as _logging

__version__ = "0.1.0"

_logger = _logging.getLogger("anidub")
if not _logger.handlers:
    _handler = _logging.StreamHandler()
    _handler.setFormatter(_logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(_logging.INFO)
    _logger.propagate = False
