from pathlib import Path

parts = sorted(Path(__file__).parent.glob("app.py.part*"))
source = "".join(part.read_text(encoding="utf-8") for part in parts)
exec(compile(source, str(Path(__file__).with_name("app.generated.py")), "exec"), globals())
