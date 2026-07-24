from pathlib import Path

base = Path(__file__).parent
parts = sorted(base.glob("app.py.part[0-9][0-9]"))
source = "".join(part.read_text(encoding="utf-8") for part in parts)
main_block = 'if __name__ == "__main__":\n    main()\n'
if not source.endswith(main_block):
    raise RuntimeError("主程序分段不完整，未找到启动入口")
source = source[: -len(main_block)]
source += "\n" + (base / "helper_fix.py").read_text(encoding="utf-8") + "\n\n" + main_block
exec(compile(source, str(base / "app.generated.py"), "exec"), globals())
