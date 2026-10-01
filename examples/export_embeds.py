"""Export illustrative contents from the actual embed builders; no Discord connection."""
import json
import tempfile
from datetime import datetime
from pathlib import Path
from bukowski.config import Config
from bukowski.store import Store, Actor
from bukowski.periods import TZ
from bukowski.branding import Brand

def main():
    at = datetime(2026,10,1,8,0,tzinfo=TZ)
    with tempfile.TemporaryDirectory() as tmp:
        config = Config('',1,{},2,frozenset({100}),frozenset({200}),str(Path(tmp)/'demo.sqlite3'),'por+eng','')
        store = Store(config.database,{100},{200},lambda:at)
        try:
            store.set_rate(Actor(1,frozenset({100})),'30')
            tid = store.manual_table(Actor(1,frozenset({100})),{'chuchu':11,'alho':10,'inhame':9},'Exemplo ilustrativo')
            brand = Brand(config)
            embeds = [brand.panel(kind,store) for kind in ('service','work','admin')]
            embeds.append(brand.prices(dict(store.one('SELECT * FROM tables WHERE id=?',(tid,))),{'chuchu':11,'alho':10,'inhame':9}))
            embeds.append(brand.quote({'withdrawal':1,'product':'alho','seeds':200,'yield':10,'units':2000,'price':10,'total':20000,'rate':'30','farm':6000,'employee':14000}))
            Path('examples/embeds.json').write_text(json.dumps([e.to_dict() for e in embeds],ensure_ascii=False,indent=2),encoding='utf-8')
            lines = ['# Exemplos finais dos embeds','', 'Conteúdo exportado do construtor real. Dados, IDs e taxa de 30% são apenas exemplos; nenhuma taxa padrão foi ativada. Não são capturas do Discord. Distintivo/faixa permanecem ausentes até serem fornecidos.','']
            for embed in embeds:
                lines.extend(['## '+embed.title,'',embed.description or '',''])
                for field in embed.fields:
                    lines.extend(['**'+field.name+'**', '',field.value,''])
                lines.extend(['*'+embed.footer.text+'*',''])
            Path('EMBEDS.md').write_text('\n'.join(lines),encoding='utf-8')
        finally:
            store.db.close()

if __name__ == '__main__':
    main()
