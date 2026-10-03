import os, sys
os.environ.setdefault('HF_HUB_DISABLE_PROGRESS_BARS','1')
os.environ.setdefault('TOKENIZERS_PARALLELISM','false')
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8', errors='replace', buffering=1)
if sys.stderr is None:
    sys.stderr = sys.stdout
if sys.stdin is None:
    sys.stdin = open(os.devnull, 'r', encoding='utf-8', errors='replace')
