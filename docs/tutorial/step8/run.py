#!/usr/bin/env python3
import os

from snakebids.app import SnakeBidsApp


def stage():
    app = SnakeBidsApp(os.path.abspath(os.path.dirname(__file__)))
    app.run_snakemake()


if __name__ == "__main__":
    stage()
