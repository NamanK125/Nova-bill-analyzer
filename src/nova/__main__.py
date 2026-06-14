"""Allow `python -m nova run ...` in addition to the installed `nova` console script."""
from nova.cli import main

if __name__ == "__main__":
    main()
