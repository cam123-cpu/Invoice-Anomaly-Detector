"""Write the sample Excel files to samples/ (run from the repository root):

    python scripts/make_samples.py
"""
import pathlib

from invoice_detector.samples import SAMPLES, build

out = pathlib.Path("samples")
out.mkdir(exist_ok=True)
for name, (title, description, _) in SAMPLES.items():
    (out / name).write_bytes(build(name))
    print(f"samples/{name}  -  {title}: {description}")
