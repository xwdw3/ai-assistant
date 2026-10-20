"""tools.py 的单元测试。"""

import pytest

import tools


class TestSafePath:
    def test_去掉路径部分(self, isolated_notes):
        path = tools._safe_path("../../逃逸.txt")
        assert path == str(isolated_notes / "逃逸.txt")

    def test_自动补后缀(self, isolated_notes):
        assert tools._safe_path("周报").endswith("周报.txt")

    @pytest.mark.parametrize("name", ["", "   ", "/", ".."])
    def test_空名兜底(self, name, isolated_notes):
        assert tools._safe_path(name).endswith(".txt")

    def test_大写后缀不重复加(self):
        assert tools._safe_path("月报.TXT").endswith("月报.TXT")
        assert not tools._safe_path("月报.TXT").endswith(".TXT.txt")


class TestSaveNote:
    def test_保存后能读回(self):
        tools.save_note("周报", "三个要点")
        assert tools.read_note("周报") == "三个要点"

    def test_同名默认不覆盖(self, isolated_notes):
        tools.save_note("周报", "第一版")
        result = tools.save_note("周报", "第二版")

        assert (isolated_notes / "周报.txt").read_text(encoding="utf-8") == "第一版"
        assert (isolated_notes / "周报_1.txt").read_text(encoding="utf-8") == "第二版"
        assert "另存为" in result

    def test_多次重名依次递增(self, isolated_notes):
        for i in range(3):
            tools.save_note("周报", f"第{i}版")

        names = sorted(p.name for p in isolated_notes.iterdir())
        assert names == ["周报.txt", "周报_1.txt", "周报_2.txt"]

    def test_显式覆盖(self, isolated_notes):
        tools.save_note("周报", "第一版")
        result = tools.save_note("周报", "第二版", overwrite=True)

        assert (isolated_notes / "周报.txt").read_text(encoding="utf-8") == "第二版"
        assert "已覆盖" in result
        assert len(list(isolated_notes.iterdir())) == 1

    def test_内容非字符串会报错(self):
        with pytest.raises(TypeError):
            tools.save_note("坏.txt", 123)

    def test_越界写入被拦住(self, isolated_notes):
        tools.save_note("../../../逃逸.txt", "x")
        assert (isolated_notes / "逃逸.txt").exists()
        assert not (isolated_notes.parent.parent / "逃逸.txt").exists()


class TestReadNote:
    def test_不存在返回提示(self):
        assert "没有找到" in tools.read_note("没这个.txt")

    def test_空文件返回空串(self):
        tools.save_note("空.txt", "")
        assert tools.read_note("空.txt") == ""

    def test_超长截断到2000(self):
        tools.save_note("长.txt", "字" * 3000)
        assert len(tools.read_note("长.txt")) == 2000


class TestListNotes:
    def test_空目录(self):
        assert tools.list_notes() == "还没有任何笔记"

    def test_列出并排序(self):
        for name in ("b", "a", "c"):
            tools.save_note(name, "x")
        listing = tools.list_notes()
        assert listing.index("a.txt") < listing.index("b.txt") < listing.index("c.txt")


class TestGetDate:
    def test_格式正确(self):
        value = tools.get_date()
        assert "年" in value and "月" in value and "日" in value
        assert "&" not in value and "%" not in value


class TestRegistry:
    def test_两份注册表名字一致(self):
        schema_names = {t["function"]["name"] for t in tools.TOOLS}
        assert schema_names == set(tools.TOOL_MAP)

    def test_声明字段与函数签名匹配(self):
        import inspect

        for item in tools.TOOLS:
            fn = tools.TOOL_MAP[item["function"]["name"]]
            params = set(item["function"]["parameters"].get("properties", {}))
            accepted = set(inspect.signature(fn).parameters)
            assert params <= accepted, f"{fn.__name__} 声明了函数不接受的参数"
