class ConfigError(Exception):
    """Exception raised for errors with the Snakebids config."""

    def __init__(self, msg):
        self.msg = msg
        super().__init__()

class RunError(Exception):
    """Exception raised for errors in generating and running the snakemake workflow."""