import pytest

from relay.extraction import ExtractionError, extract, extract_number, parse_path


class TestParsePath:
    def test_simple_key(self):
        assert parse_path("value") == ["value"]

    def test_dotted_keys(self):
        assert parse_path("data.value") == ["data", "value"]

    def test_index(self):
        assert parse_path("items[0]") == ["items", 0]

    def test_dotted_then_index(self):
        assert parse_path("data.items[2].value") == ["data", "items", 2, "value"]

    def test_chained_indices(self):
        assert parse_path("matrix[0][1]") == ["matrix", 0, 1]

    @pytest.mark.parametrize("path", ["", "   ", "a..b", ".a", "a.", "a[", "a[x]", "a[1"])
    def test_malformed_paths_rejected(self, path):
        with pytest.raises(ExtractionError):
            parse_path(path)


class TestExtract:
    def test_simple_object(self):
        assert extract({"value": 42}, "value") == 42

    def test_nested_object(self):
        data = {"data": {"value": 3.14}}
        assert extract(data, "data.value") == 3.14

    def test_array_index(self):
        data = {"items": [10, 20, 30]}
        assert extract(data, "items[1]") == 20

    def test_mixed_path(self):
        data = {"data": {"items": [{"value": 7}]}}
        assert extract(data, "data.items[0].value") == 7

    def test_missing_key_raises(self):
        with pytest.raises(ExtractionError):
            extract({"a": 1}, "b")

    def test_index_out_of_range_raises(self):
        with pytest.raises(ExtractionError):
            extract({"items": [1]}, "items[5]")

    def test_index_into_non_list_raises(self):
        with pytest.raises(ExtractionError):
            extract({"items": {"a": 1}}, "items[0]")

    def test_key_into_non_dict_raises(self):
        with pytest.raises(ExtractionError):
            extract({"items": [1, 2]}, "items.value")


class TestExtractNumber:
    def test_int_value(self):
        assert extract_number({"v": 5}, "v") == 5.0

    def test_float_value(self):
        assert extract_number({"v": 5.5}, "v") == 5.5

    def test_numeric_string_coerced(self):
        assert extract_number({"v": "5.5"}, "v") == 5.5

    def test_non_numeric_string_rejected(self):
        with pytest.raises(ExtractionError):
            extract_number({"v": "hello"}, "v")

    def test_bool_rejected(self):
        with pytest.raises(ExtractionError):
            extract_number({"v": True}, "v")

    def test_null_rejected(self):
        with pytest.raises(ExtractionError):
            extract_number({"v": None}, "v")

    def test_object_rejected(self):
        with pytest.raises(ExtractionError):
            extract_number({"v": {"nested": 1}}, "v")
