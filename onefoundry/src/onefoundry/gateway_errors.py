"""Typed Tool Gateway boundary failures shared by focused stores."""


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Unavailable(Exception):
    pass
