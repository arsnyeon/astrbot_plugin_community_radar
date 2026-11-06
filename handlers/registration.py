"""Bind split handlers to AstrBot's entry-module registration namespace.

AstrBot 4.29 binds decorator handlers by their __module__, not the class MRO.
Apply this BEFORE AstrBot decorators (closest decorator to the function).
"""


def entry_handler(function):
    package = function.__module__.split('.handlers.', 1)[0]
    function.__module__ = package + '.main'
    return function
