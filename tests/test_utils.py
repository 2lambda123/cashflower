from unittest import TestCase

from cashflower.utils import get_first_indexes, get_object_by_name, print_log, split_to_ranges, updt


class TestSplitToRanges(TestCase):
    def test_split_to_ranges(self):
        assert split_to_ranges(20, 3) == [(0, 6), (6, 12), (12, 20)]
        assert split_to_ranges(2, 3) == [(0, 2)]


class TestGetObjectByName(TestCase):
    def test_get_object_by_name(self):
        class Object:
            def __init__(self, name):
                self.name = name

        a = Object(name="a")
        b = Object(name="b")
        objects = [a, b]

        assert get_object_by_name(objects, "a") == a
        assert get_object_by_name(objects, "c") is None


class TestPrintFunctions(TestCase):
    def test_print_functions(self):
        assert updt(100, 20) is None
        assert updt(100, 110) is None
        assert print_log("my message") is None


class TestGetFirstIndexes(TestCase):
    def test_get_first_indexes(self):
        assert get_first_indexes(["A", "A", "B", "A", "C", "D"]) == [0, 2, 4, 5]
