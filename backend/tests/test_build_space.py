"""build_space's privacy gate. Spec: DESIGN.md §4.10 (step 15 decisions)."""

from scripts.build_space import check


def _bundle(tmp_path, files: dict[str, str]):
    paths = []
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        paths.append(p)
    return check(tmp_path, paths)


def test_clean_bundle_passes(tmp_path):
    assert _bundle(tmp_path, {
        "backend/app/x.py": 'T = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")',
        "backend/seed/ATTRIBUTION.csv": "f,https://www.flickr.com/photos/12345678@N00/53783096186,x",
        "web/app/page.tsx": 'placeholder="Rs 12,500"'}) == []


def test_private_things_are_refused(tmp_path):
    problems = _bundle(tmp_path, {
        ".env": "X=1",
        "backend/app/a.py": "# call 0300 1234567",
        "backend/app/b.py": "JID = '923001234567@s.whatsapp.net'",
        "backend/app/c.py": "api_key = 'abcdefghijklmnopqrstuvwxyz0123'",
        "data/media/x.txt": "x"})
    joined = "\n".join(problems)
    for rel in (".env", "backend/app/a.py", "backend/app/b.py", "backend/app/c.py", "data/media/x.txt"):
        assert rel in joined
