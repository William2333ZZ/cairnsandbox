# robot-name

# Instructions

Manage robot factory settings.

When a robot comes off the factory floor, it has no name.

The first time you turn on a robot, a random name is generated in the format of two uppercase letters followed by three digits, such as RX837 or BC811.

Every once in a while we need to reset a robot to its factory settings, which means that its name gets wiped.
The next time you ask, that robot will respond with a new random name.

The names must be random: they should not follow a predictable sequence.
Using random names means a risk of collisions.
Your solution must ensure that every existing robot has a unique name.

## Task

Implement the solution in `robot_name.py` (the stub is already there). Hidden unit tests exercise the public functions and classes named in the stub; keep their names and signatures. You may add your own tests, but do not rely on any test file being present. Only edit `robot_name.py`.
