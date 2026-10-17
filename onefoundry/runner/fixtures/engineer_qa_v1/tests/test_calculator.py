import unittest

from calculator import add_one


class CalculatorTest(unittest.TestCase):
    def test_add_one(self) -> None:
        self.assertEqual(add_one(2), 3)


if __name__ == "__main__":
    unittest.main()
