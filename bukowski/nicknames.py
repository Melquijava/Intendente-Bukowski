from string import Formatter
from .store import DomainError

def validate_rules(rules, template):
    fields = [field for _,field,_,_ in Formatter().parse(template) if field is not None]
    if sorted(fields) != ['document','name','prefix']:
        raise ValueError('NICKNAME_FORMAT deve conter uma vez {prefix}, {name} e {document}.')
    if any(spec or conversion for _,_,spec,conversion in Formatter().parse(template)):
        raise ValueError('Formato de apelido não aceita conversões ou especificadores.')
    seen = set()
    for rule in rules:
        if not isinstance(rule.get('role_id'),int) or rule['role_id'] <= 0 or rule['role_id'] in seen:
            raise ValueError('Mapa de apelidos exige IDs de cargos positivos e únicos.')
        seen.add(rule['role_id'])
        if not isinstance(rule.get('priority'),int) or not isinstance(rule.get('prefix'),str) or not rule['prefix'].strip() or len(rule['prefix']) > 8:
            raise ValueError('Cada cargo mapeado exige prefixo explícito (até 8 caracteres) e prioridade inteira.')

def nickname(name, document, role_ids, rules, template='{prefix} | {name} | {document}'):
    validate_rules(rules,template)
    matching = [r for r in rules if r['role_id'] in role_ids]
    if not matching:
        return None  # No invented abbreviation; retain the existing nickname.
    top = sorted(matching,key=lambda r:(-r['priority'],r['role_id']))[0]
    prefix = top['prefix']
    overhead = len(template.format(prefix=prefix,name='',document=document))
    available = 32-overhead
    if available < 1:
        raise DomainError('Prefixo e documento não cabem em 32 caracteres. Revise o documento ou formato; eles não serão truncados.')
    short_name = ' '.join(name.split())[:available].rstrip()
    return template.format(prefix=prefix,name=short_name,document=document)
