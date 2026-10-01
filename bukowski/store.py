import asyncio
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from pathlib import Path
from .periods import now, period
from .ocr import product

class DomainError(ValueError):
    pass

@dataclass(frozen=True)
class Actor:
    id: int
    roles: frozenset = frozenset()



class Store:
    def __init__(self, path, admins=(), finance=(), clock=now):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.migrate()
        self.admins, self.finance, self.clock = set(admins), set(finance), clock
        self.approval_lock = asyncio.Lock()

    def migrate(self):
        self.db.execute('CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY)')
        folder = Path(__file__).parent / 'migrations'
        files = sorted(folder.glob('[0-9]*.sql'))
        known = {int(file.name.split('_')[0]) for file in files}
        applied = {r['version'] for r in self.db.execute('SELECT version FROM migrations')}
        if applied - known:
            raise DomainError('Banco usa uma vers?o mais nova; n?o fa?a downgrade do bot.')
        for file in files:
            version = int(file.name.split('_')[0])
            if version not in applied:
                try:
                    self.db.executescript('BEGIN IMMEDIATE;\n' + file.read_text(encoding='utf-8') + '\nCOMMIT;')
                except BaseException:
                    if self.db.in_transaction:
                        self.db.execute('ROLLBACK')
                    raise

    @contextmanager
    def tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def audit(self, actor, event, payload):
        self.db.execute('INSERT INTO audit(actor,event,payload,created) VALUES(?,?,?,?)', (actor.id, event, json.dumps(payload, ensure_ascii=False), self.clock().isoformat()))

    def admin(self, actor):
        if not self.admins.intersection(actor.roles):
            raise DomainError('Somente cargos administrativos configurados podem realizar esta ação.')

    def cashier(self, actor):
        if not self.finance.intersection(actor.roles):
            raise DomainError('Somente cargos financeiros configurados podem confirmar recebimentos.')

    def approved(self, actor):
        if not self.one("SELECT id FROM registrations WHERE user=? AND status='approved'", (actor.id,)):
            raise DomainError('Seu cadastro precisa estar aprovado.')

    @staticmethod
    def identity(name, document, mail):
        if not name.strip() or len(name) > 100 or not document.isascii() or not document.isdigit() or not mail.isascii() or not mail.isdigit() or max(len(document), len(mail)) > 40:
            raise DomainError('Informe nome e documento/correio numéricos (até 40 dígitos). Zeros iniciais são preservados.')

    def register(self, actor, name, document, mail):
        self.identity(name, document, mail)
        try:
            with self.tx():
                cur = self.db.execute('INSERT INTO registrations(user,name,document,mail,created) VALUES(?,?,?,?,?)', (actor.id,name.strip(),document,mail,self.clock().isoformat()))
                self.audit(actor,'cadastro_solicitado',{'id':cur.lastrowid})
                return cur.lastrowid
        except sqlite3.IntegrityError:
            raise DomainError('Cadastro ativo duplicado ou documento em conflito. Solicite revisão administrativa.')

    async def approve(self, actor, registration, grant_role):
        self.admin(actor)
        async with self.approval_lock:
            row = self.one('SELECT * FROM registrations WHERE id=?', (registration,))
            if not row or row['status'] != 'pending':
                raise DomainError('Solicitação já decidida ou inexistente.')
            try:
                await grant_role(row['user'])
            except Exception:
                self.audit(actor,'cargo_falhou',{'registration':registration})
                raise DomainError('Falha ao conceder P1. Cadastro continua pendente; corrija permissões e tente novamente.')
            with self.tx():
                updated = self.db.execute("UPDATE registrations SET status='approved',decider=?,decided=? WHERE id=? AND status='pending'",(actor.id,self.clock().isoformat(),registration))
                if not updated.rowcount:
                    raise DomainError('O cadastro mudou durante a concessão de P1 (possível saída do servidor). Revise antes de tentar novamente.')
                self.audit(actor,'cadastro_aprovado',{'registration':registration})
            return row['user']

    async def reject(self, actor, registration, reason):
        self.admin(actor)
        self.reason(reason)
        async with self.approval_lock:
            with self.tx():
                row = self.one("SELECT * FROM registrations WHERE id=? AND status='pending'",(registration,))
                if not row:
                    raise DomainError('Solicitação já decidida ou inexistente.')
                self.db.execute("UPDATE registrations SET status='rejected',decider=?,decided=?,reason=? WHERE id=?",(actor.id,self.clock().isoformat(),reason,registration))
                self.audit(actor,'cadastro_recusado',{'registration':registration,'reason':reason})
                return row['user']

    def correct_registration(self, actor, registration, name, document, mail, reason):
        self.admin(actor)
        self.identity(name,document,mail)
        self.reason(reason)
        with self.tx():
            old = self.one('SELECT * FROM registrations WHERE id=?',(registration,))
            if not old:
                raise DomainError('Cadastro inexistente.')
            try:
                self.db.execute('UPDATE registrations SET name=?,document=?,mail=? WHERE id=?',(name,document,mail,registration))
            except sqlite3.IntegrityError:
                raise DomainError('Documento em conflito; resolva o cadastro duplicado antes.')
            self.audit(actor,'cadastro_corrigido',{'before':dict(old),'after':[name,document,mail],'reason':reason})

    def set_rate(self, actor, value):
        self.admin(actor)
        try:
            rate = Decimal(value.replace(',','.'))
            if not rate.is_finite() or not 0 <= rate <= 100 or rate.as_tuple().exponent < -6:
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise DomainError('Taxa deve ser de 0 a 100%, com até seis casas decimais.')
        with self.tx():
            old = self.get_setting('rate')
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('rate',?)",(str(rate),))
            self.audit(actor,'taxa_definida',{'before':old,'after':str(rate)})

    def get_setting(self, key):
        r = self.one('SELECT value FROM settings WHERE key=?',(key,))
        return r['value'] if r else None

    def set_yield(self, actor, name, units):
        self.admin(actor)
        if not 1 <= units <= 1_000_000:
            raise DomainError('Rendimento deve ser inteiro entre 1 e 1000000.')
        name = product(name)
        with self.tx():
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('yield:'+name,str(units)))
            self.audit(actor,'rendimento_definido',{'product':name,'yield':units})

    def begin_table(self, message, actor, attachment, published):
        with self.tx():
            cur = self.db.execute("INSERT OR IGNORE INTO tables(message,author,attachment,published,period,state) VALUES(?,?,?,?,?,'processing')",(str(message),actor.id,attachment,published.isoformat(),period(published)))
            return cur.lastrowid if cur.rowcount else None

    def finish_table(self, table_id, actor, raw, prices, error=None):
        with self.tx():
            row = self.one('SELECT * FROM tables WHERE id=?',(table_id,))
            if not row or row['state'] != 'processing':
                raise DomainError('Extração já finalizada.')
            state = 'expired' if row['period'] != period(self.clock()) else 'pending' if error or not self.admins.intersection(actor.roles) else 'valid'
            self.db.execute('UPDATE tables SET raw=?,prices=?,error=?,state=? WHERE id=?',(raw,json.dumps(prices),error,state,table_id))
            self.audit(actor,'tabela_extraida',{'id':table_id,'state':state,'error':error})
            return state

    def manual_table(self, actor, prices, reason, table_id=None):
        self.admin(actor)
        self.reason(reason)
        if not prices or any(not isinstance(v,int) or v <= 0 for v in prices.values()):
            raise DomainError('Tabela precisa de produtos e preços positivos em centavos.')
        with self.tx():
            if table_id is None:
                t = self.clock()
                cur = self.db.execute("INSERT INTO tables(author,published,period,raw,state,prices) VALUES(?,?,?,?,'valid',?)",(actor.id,t.isoformat(),period(t),reason,json.dumps(prices)))
                table_id = cur.lastrowid
                before = None
            else:
                old = self.one('SELECT * FROM tables WHERE id=?',(table_id,))
                if not old or old['period'] != period(self.clock()) or old['state'] not in ('pending','processing'):
                    raise DomainError('Somente tabela pendente do período atual pode ser validada.')
                before = dict(old)
                self.db.execute("UPDATE tables SET prices=?,state='valid',error=NULL WHERE id=?",(json.dumps(prices),table_id))
            self.audit(actor,'tabela_manual_validada',{'id':table_id,'prices':prices,'reason':reason,'before':before})
            return table_id

    def current_table(self):
        row = self.one("SELECT * FROM tables WHERE period=? AND state='valid' ORDER BY julianday(published) DESC,id DESC LIMIT 1",(period(self.clock()),))
        if not row:
            raise DomainError('Aguardando nova tabela em alta.')
        return row, json.loads(row['prices'])

    def withdraw(self, actor, name, seeds, operation):
        self.approved(actor)
        name = product(name)
        if not 1 <= seeds <= 1_000_000:
            raise DomainError('Quantidade deve ser inteira entre 1 e 1000000.')
        with self.tx():
            _, prices = self.current_table()
            rate = self.get_setting('rate')
            if rate is None:
                raise DomainError('A gerência precisa configurar a taxa antes de liberar retiradas.')
            if name not in prices:
                raise DomainError('Produto ausente da tabela vigente.')
            units = int(self.get_setting('yield:'+name) or '10')
            try:
                cur = self.db.execute('INSERT INTO withdrawals(user,product,seeds,yield,rate,created,operation) VALUES(?,?,?,?,?,?,?)',(actor.id,name,seeds,units,rate,self.clock().isoformat(),str(operation)))
            except sqlite3.IntegrityError:
                raise DomainError('Esta retirada já foi registrada.')
            self.audit(actor,'sementes_retiradas',{'id':cur.lastrowid,'seeds':seeds,'yield':units,'rate':rate})
            return cur.lastrowid

    def quote(self, actor, withdrawal):
        self.approved(actor)
        with self.tx():
            w = self.one("SELECT * FROM withdrawals WHERE id=? AND user=? AND state='open'",(withdrawal,actor.id))
            if not w:
                raise DomainError('Retirada inexistente, fechada ou de outro funcionário.')
            table, prices = self.current_table()
            price = prices.get(w['product'])
            if price is None:
                raise DomainError('Produto ausente da tabela vigente.')
            total = w['seeds'] * w['yield'] * price
            if total > 9_000_000_000_000_000:
                raise DomainError('Total excede o limite financeiro suportado. Solicite revisão administrativa.')
            farm = int((Decimal(total)*Decimal(w['rate'])/100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
            cur = self.db.execute('INSERT INTO quotes(withdrawal,table_id,price,total,farm,created) VALUES(?,?,?,?,?,?)',(withdrawal,table['id'],price,total,farm,self.clock().isoformat()))
            return {'id':cur.lastrowid,'withdrawal':withdrawal,'units':w['seeds']*w['yield'],'price':price,'total':total,'farm':farm,'employee':total-farm,'rate':w['rate']}

    def sell(self, actor, quote_id):
        self.approved(actor)
        with self.tx():
            q = self.one('SELECT q.*,w.user,w.state FROM quotes q JOIN withdrawals w ON w.id=q.withdrawal WHERE q.id=?',(quote_id,))
            if not q or q['user'] != actor.id or q['state'] != 'open':
                raise DomainError('Retirada já vendida ou confirmação inválida.')
            table,_ = self.current_table()
            if table['id'] != q['table_id']:
                raise DomainError('Tabela mudou. Consulte novamente o cálculo antes de confirmar.')
            cur = self.db.execute('INSERT INTO sales(withdrawal,table_id,price,total,farm,employee,created) VALUES(?,?,?,?,?,?,?)',(q['withdrawal'],q['table_id'],q['price'],q['total'],q['farm'],q['total']-q['farm'],self.clock().isoformat()))
            payment = self.db.execute('INSERT INTO payments(sale,user,amount) VALUES(?,?,?)',(cur.lastrowid,actor.id,q['farm'])).lastrowid
            self.db.execute("UPDATE withdrawals SET state='sold' WHERE id=?",(q['withdrawal'],))
            self.audit(actor,'venda_calculada',{'sale':cur.lastrowid,'payment':payment,'quote':quote_id})
            return payment

    def report_payment(self, actor, payment, receiver):
        with self.tx():
            r = self.db.execute("UPDATE payments SET state='reported',receiver=?,reported_at=? WHERE id=? AND user=? AND state='pending'",(receiver,self.clock().isoformat(),payment,actor.id))
            if not r.rowcount:
                raise DomainError('Repasse inexistente, já informado ou de outro funcionário.')
            self.audit(actor,'entrega_informada',{'payment':payment,'receiver':receiver})

    def receive(self, actor, payment, accept, reason=''):
        self.cashier(actor)
        if not accept:
            self.reason(reason)
        with self.tx():
            p = self.one("SELECT * FROM payments WHERE id=? AND state='reported'",(payment,))
            if not p:
                raise DomainError('Entrega não informada ou recebimento já decidido.')
            if accept:
                self.db.execute("UPDATE payments SET state='received',confirmed_by=?,confirmed_at=? WHERE id=?",(actor.id,self.clock().isoformat(),payment))
                self.db.execute('INSERT INTO ledger(payment,amount,actor,receiver,created,reason) VALUES(?,?,?,?,?,?)',(payment,p['amount'],actor.id,p['receiver'],self.clock().isoformat(),'Recebimento confirmado'))
            else:
                self.db.execute("UPDATE payments SET state='pending',receiver=NULL,reported_at=NULL WHERE id=?",(payment,))
            self.audit(actor,'recebimento_confirmado' if accept else 'recebimento_rejeitado',{'payment':payment,'reason':reason,'receiver':p['receiver'],'amount':p['amount']})

    @staticmethod
    def reason(reason):
        if not reason.strip():
            raise DomainError('Informe um motivo para a revisão.')

    def adjust(self, actor, cents, reason, reverse=None):
        self.cashier(actor)
        self.reason(reason)
        with self.tx():
            if reverse is not None:
                row = self.one('SELECT * FROM ledger WHERE id=?',(reverse,))
                if not row or self.one('SELECT id FROM ledger WHERE reverses=?',(reverse,)):
                    raise DomainError('Lançamento inexistente ou já estornado.')
                cents = -row['amount']
            if not isinstance(cents,int) or cents == 0:
                raise DomainError('Ajuste deve ser um valor não zero em centavos.')
            cur = self.db.execute('INSERT INTO ledger(amount,actor,created,reason,reverses) VALUES(?,?,?,?,?)',(cents,actor.id,self.clock().isoformat(),reason,reverse))
            self.audit(actor,'caixa_ajustado',{'ledger':cur.lastrowid,'amount':cents,'reason':reason,'reverses':reverse})
            return cur.lastrowid

    def cancel_withdrawal(self, actor, withdrawal, reason):
        self.admin(actor)
        self.reason(reason)
        with self.tx():
            cur = self.db.execute("UPDATE withdrawals SET state='cancelled' WHERE id=? AND state='open'",(withdrawal,))
            if not cur.rowcount:
                raise DomainError('Somente retirada aberta pode ser cancelada. Vendas exigem ajuste financeiro separado.')
            self.audit(actor,'retirada_cancelada',{'id':withdrawal,'reason':reason})

    def balance(self, actor):
        self.cashier(actor)
        return self.one('SELECT COALESCE(SUM(amount),0) AS total FROM ledger')['total']
