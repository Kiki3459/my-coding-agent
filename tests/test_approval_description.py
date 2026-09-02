import unittest

from mini_agent.approval_description import describe_operation


class ApprovalDescriptionTests(unittest.TestCase):
    def test_python_test(self):
        self.assertIn("运行 Python 测试", describe_operation("bash", {"command": "python -m unittest -v"}))

    def test_multiple_commands(self):
        text = describe_operation("bash", {"command": "find . -type f | sort; ls -la"})
        for expected in ("组合命令", "查找", "排序", "查看目录"):
            self.assertIn(expected, text)

    def test_deletion_is_not_described_as_reading(self):
        text = describe_operation("bash", {"command": "ls; rm sample.txt"})
        self.assertIn("删除", text)
        self.assertIn("附加操作", describe_operation("bash", {"command": "find . -delete"}))

    def test_opaque_shell_is_not_given_a_safe_label(self):
        for command in ("ls $(rm sample.txt)", "cat a > b", "echo `id`", "ls\nrm a"):
            self.assertIn("脚本、展开或重定向", describe_operation("bash", {"command": command}))

    def test_unknown_and_invalid_commands(self):
        self.assertIn("custom-tool", describe_operation("bash", {"command": "custom-tool --flag"}))
        self.assertIn("完整命令", describe_operation("bash", {"command": "ls '"}))

    def test_file_descriptions(self):
        self.assertIn("精确替换", describe_operation("edit", {"path": "calculator.py"}))
        text = describe_operation("write", {"path": "report.md"})
        self.assertIn("report.md", text)
        self.assertIn("覆盖", text)

    def test_compilation(self):
        self.assertIn("语法", describe_operation("bash", {"command": "clang++ -fsyntax-only test.cpp"}))


if __name__ == "__main__":
    unittest.main()
