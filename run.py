#!/usr/bin/env python
"""더블클릭/단축 실행용 진입점.

`python run.py` 는 `python -m app.main` 과 동일하게 AssForge 를 실행한다.
"""
from app.main import main

if __name__ == "__main__":
    main()
