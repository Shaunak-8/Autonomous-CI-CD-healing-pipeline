import textwrap

from whitebox import WhiteBoxAnalyzer


def test_whitebox_analyzer_detects_python_functions_and_branches(tmp_path):
    file_path = tmp_path / "validator.py"
    file_path.write_text(
        textwrap.dedent(
            '''
            def validate(age):
                if age < 18:
                    return False
                if age > 100:
                    return False
                return True
            '''
        )
    )

    analyzer = WhiteBoxAnalyzer(str(tmp_path))
    report = analyzer.analyze_python_file(str(file_path))

    assert any(item["name"] == "validate" for item in report["functions"])
    assert any("age < 18" in item.get("condition", "") for item in report["branches"])
    assert any("age > 100" in item.get("condition", "") for item in report["branches"])


def test_whitebox_handles_missing_coverage_data(tmp_path):
    analyzer = WhiteBoxAnalyzer(str(tmp_path))
    report = analyzer.collect_coverage(str(tmp_path), allow_missing=True)

    assert report["enabled"] is True
    assert report["status"] in {"PARTIAL", "UNSUPPORTED", "OK"}
    assert isinstance(report.get("reason", ""), str)


def test_whitebox_generates_targeted_test_for_uncovered_branch(tmp_path):
    source = tmp_path / "validator.py"
    source.write_text(
        textwrap.dedent(
            '''
            def validate(age):
                if age < 18:
                    return False
                return True
            '''
        )
    )

    analyzer = WhiteBoxAnalyzer(str(tmp_path))
    branch = {"file": "validator.py", "function": "validate", "line": 2, "condition": "age < 18", "suggested_input": "age = 15"}
    generated = analyzer.generate_targeted_test(branch, str(source))

    assert "validate(15)" in generated or "age = 15" in generated
    assert "assert" in generated


def test_whitebox_reports_unsupported_language(tmp_path):
    file_path = tmp_path / "example.rb"
    file_path.write_text("puts 'hello'\n")

    analyzer = WhiteBoxAnalyzer(str(tmp_path))
    report = analyzer.analyze_language(str(file_path))

    assert report["enabled"] is True
    assert report["status"] == "UNSUPPORTED"
    assert "not supported" in report["reason"].lower()
