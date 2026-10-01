import os
import json
import logging
from dataclasses import dataclass
from dotenv import load_dotenv

@dataclass
class Config:
    token: str
    guild: int
    channels: dict
    peao: int
    admins: frozenset
    finance: frozenset
    database: str
    ocr_lang: str
    tesseract: str
    nickname_roles: tuple = ()
    nickname_format: str = '{prefix} | {name} | {document}'
    badge_url: str = ''
    banner_url: str = ''
    ocr_backend: str = 'tesseract'
    vision_model: str = 'gemini-2.5-flash'
    vision_key: str = ''

    def role_rules(self):
        return self.nickname_roles or ({'role_id':self.peao,'prefix':'P','priority':10},)

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
        peao = os.environ.get('PEAO_ROLE_ID','') or os.environ.get('P1_ROLE_ID','')
        if not peao or int(peao) <= 0:
            raise ValueError('Configure PEAO_ROLE_ID com o ID do cargo Peão.')
        if not os.environ.get('PEAO_ROLE_ID'):
            logging.getLogger(__name__).warning('Configuração antiga P1_ROLE_ID aceita como Peão; migre para PEAO_ROLE_ID.')
        peao = int(peao)
        rules = [{'role_id':peao,'prefix':'P','priority':10}]
        manager = os.environ.get('GERENTE_ROLE_ID','').strip()
        if manager:
            rules.append({'role_id':int(manager),'prefix':'G','priority':100})
        custom = os.environ.get('NICKNAME_ROLE_MAP','').strip()
        if custom:
            rules = json.loads(custom)
        from .nicknames import validate_rules
        fmt = os.environ.get('NICKNAME_FORMAT','{prefix} | {name} | {document}')
        validate_rules(rules,fmt)
        for key in ('BRAND_BADGE_URL','BRAND_BANNER_URL'):
            value = os.environ.get(key,'')
            if value and not value.startswith('https://'):
                raise ValueError(f'{key}: use uma URL HTTPS real ou deixe vazio.')
        backend = os.environ.get('OCR_BACKEND','tesseract')
        if backend not in ('tesseract','gemini'):
            raise ValueError('OCR_BACKEND deve ser tesseract ou gemini.')
        return cls(token, ident('GUILD_ID'), {n: ident(n+'_ID') for n in names}, peao, roles('ADMIN_ROLE_IDS'), roles('FINANCE_ROLE_IDS'), os.environ.get('DATABASE_PATH', 'data/bukowski.sqlite3'), os.environ.get('OCR_LANG', 'por+eng'), os.environ.get('TESSERACT_CMD', ''),tuple(rules),fmt,os.environ.get('BRAND_BADGE_URL',''),os.environ.get('BRAND_BANNER_URL',''),backend,os.environ.get('VISION_MODEL','gemini-2.5-flash'),os.environ.get('GEMINI_API_KEY',''))
