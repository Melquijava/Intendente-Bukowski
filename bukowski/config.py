import os
from dataclasses import dataclass
from dotenv import load_dotenv

@dataclass
class Config:
    token: str
    guild: int
    channels: dict
    p1: int
    admins: frozenset
    finance: frozenset
    database: str
    ocr_lang: str
    tesseract: str

    @classmethod
    def load(cls):
        load_dotenv()
        def ident(key):
            value = int(os.environ.get(key, '0') or '0')
            if value <= 0:
                raise ValueError(f"Configure {key} com um ID Discord válido.")
            return value
        def roles(key):
            result = frozenset(int(x.strip()) for x in os.environ.get(key, '').split(',') if x.strip())
            if not result:
                raise ValueError(f"Configure {key}.")
            return result
        names = ['PUBLIC_CATEGORY', 'INFO_CHANNEL', 'ANNOUNCEMENTS_CHANNEL', 'RULES_CHANNEL', 'SERVICE_CHANNEL', 'PRICES_CHANNEL', 'WORK_CHANNEL', 'REQUESTS_CHANNEL', 'LOG_CHANNEL', 'ARRIVALS_CHANNEL']
        token = os.environ.get('DISCORD_TOKEN', '')
        if not token:
            raise ValueError('Configure DISCORD_TOKEN no ambiente.')
        return cls(token, ident('GUILD_ID'), {n: ident(n+'_ID') for n in names}, ident('P1_ROLE_ID'), roles('ADMIN_ROLE_IDS'), roles('FINANCE_ROLE_IDS'), os.environ.get('DATABASE_PATH', 'data/bukowski.sqlite3'), os.environ.get('OCR_LANG', 'por+eng'), os.environ.get('TESSERACT_CMD', ''))
