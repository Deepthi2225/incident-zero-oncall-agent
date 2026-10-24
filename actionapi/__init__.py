"""Our own Action API (mentor-confirmed): the only component that changes the demo.

A separate local service on 127.0.0.1. The agent (oncall/) never imports it and reaches it only over
HTTP, through oncall/action_api.py.
"""
