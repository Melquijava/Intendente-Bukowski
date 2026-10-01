import logging
from .config import Config
from .bot import Bukowski

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
config = Config.load()
Bukowski(config).run(config.token,log_handler=None)
