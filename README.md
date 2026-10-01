# Intendente Bukowski — Guardião da Fazenda

Primeira etapa para a Fazenda Bukowski no Eldorado Roleplay (RedM). Python 3.11+, discord.py, SQLite persistente e OCR local Tesseract. Não contém módulos de animais, guerras ou encomendas.

## Instalação local

No PowerShell, dentro deste projeto:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock.txt
Copy-Item .env.example .env
# Edite .env localmente; não compartilhe o token.
.\.venv\Scripts\python -m bukowski
```

Instale [Tesseract para Windows](https://github.com/UB-Mannheim/tesseract/wiki) em uma pasta exclusiva e os modelos `por` e `eng`. Configure `TESSERACT_CMD` com o caminho absoluto para `tesseract.exe` caso esteja fora do PATH. Em Debian/Ubuntu: `sudo apt-get install tesseract-ocr tesseract-ocr-por tesseract-ocr-eng`. Confira `tesseract --list-langs`. O pacote Python pytesseract não instala o executável. Referência: [instalação oficial do OCR](https://tesseract-ocr.github.io/tessdoc/Installation.html).

Preencha todos os IDs de `.env.example` (Modo desenvolvedor do Discord → Copiar ID). `ADMIN_ROLE_IDS` e `FINANCE_ROLE_IDS` são listas separadas por vírgula. Um gerente que precise fazer ambas as ações deve ter cargo incluído em ambas as listas. Permissão Administrador do Discord não substitui essas listas. O banco local fica em `data/bukowski.sqlite3`; nunca publique esse arquivo ou `.env`.

## Configuração manual do Discord

1. Crie a aplicação/bot no Developer Portal. Convide com escopos `bot` e `applications.commands`.
2. Ative **Server Members Intent** e **Message Content Intent** no portal. O código usa apenas guilds, members, guild_messages e message_content. O último permite ler anexos do canal em-alta; members habilita entrada/saída e identidade. Veja [intents do discord.py](https://discordpy.readthedocs.io/en/stable/intents.html).
3. Crie/configure manualmente a categoria pública Fazenda Post e canais informações, avisos, regras e atendimento. Todos podem visualizar esses espaços; somente a gerência publica informações/regras/avisos. Atendimento deve permitir ao bot publicar o painel.
4. Configure em-alta e painel-trabalho para P1/gerência, negando **Ver canal** a `@everyone` e a cargos de não aprovados. Não dê P1 automaticamente na entrada. P1 não pode ter Administrador. Membros aprovados recebem P1 pelo bot; quem sai deve cadastrar-se novamente, mantendo o histórico anterior.
5. Solicitações administrativas e registros administrativos devem negar **Ver canal** a `@everyone`, P1 e quaisquer cargos comuns. Permita somente cargos administrativos/financeiros e o bot. Documento e correio são publicados apenas nas solicitações privadas ou em respostas efêmeras administrativas. O bot recusa inicialização se os canais de trabalho/admin estiverem públicos ou se P1 puder acessar canais administrativos; revise também outros cargos e permissões individuais manualmente.
6. Configure canal entrada/saída e todos os IDs. A categoria pública é apenas uma referência de configuração; o bot não altera sua estrutura.
7. Ao bot conceda **Ver canais**, **Enviar mensagens**, **Inserir links (Embed Links)**, **Ler histórico de mensagens** nos canais configurados, e **Gerenciar cargos** no servidor. O cargo do bot precisa estar acima de P1. Não conceda Administrador, Gerenciar canais, Expulsar/Banir ou Gerenciar servidor. Não usa auditoria do Discord: todas as despedidas dizem apenas “saída”, sem inferir banimento/expulsão.
8. Execute `/instalar` com cargo administrativo: cria/atualiza os três painéis. IDs das mensagens são persistidos e o histórico é reconciliado para recuperar instalação interrompida. Repetir não duplica; não apaga, move ou recria canais. Se um painel for apagado manualmente, `/instalar` o repõe. Alteração do canal de um painel existente exige revisão manual da configuração.
9. Execute `/taxa definir percentual:<taxa real acordada>` antes de liberar retiradas. **Não existe taxa padrão.** `/rendimento definir` altera rendimento por produto; o inicial é 10 unidades por semente.

Teste no servidor de homologação com um usuário sem aprovação, P1 e gerente. Verifique visibilidade dos canais, hierarquia de P1, DM bloqueada e ações de usuários sem cargos autorizados. Esses testes reais exigem token/servidor e não são substituídos pelos testes automatizados.

## Operação

**Cadastro:** botão “Solicitar registro” → formulário → solicitação privada com avatar e Aprovar/Recusar. Nome, documento e correio estão associados ao Discord. Documento/correio são texto numérico e preservam zeros. Duplicados ativos/documentos conflitantes bloqueiam nova solicitação para revisão; o gerente pode consultar `/cadastros`, corrigir `/cadastro_corrigir` com motivo e republicar `/solicitacao`. Aprovação verifica cargos no backend e só termina após conceder P1. Falha mantém pendente para nova tentativa. Recusa exige motivo. DM bloqueada gera aviso público genérico, sem dados pessoais, e consulta privada pelo painel. Botões antigos seguem a decisão única.

**Preços:** publique uma única imagem de até 10 MB por mensagem em em-alta. OCR executado fora do loop Discord, limitado a dois trabalhos simultâneos e 30 segundos por leitura. O leitor é substituível pela interface `Reader` e injeção no cliente. Não há API externa, chave ou cobrança por imagem; há consumo dos recursos de hospedagem. A extração aceita linhas `Produto: $0.10` ou `Produto: $0,10`. Títulos, ruído ou linhas não interpretadas provocam revisão da tabela inteira; não há preenchimento automático ou publicação parcial. Prints com layout diferente precisam de `/tabela manual` (uma linha por produto, motivo e opcional ID pendente).

Autores sem cargo administrativo geram tabela pendente, mesmo quando o OCR lê os preços. A gerência usa `/tabelas` e `/tabela manual` para validar. Salva autor, mensagem, anexo, horário, período, texto OCR, produtos/preços e estado. O anexo é referenciado por ID/URL/nome; a URL do Discord pode expirar. Tabelas corrigidas preservam antes/depois na auditoria. O último horário de publicação válido vence, mesmo se o processamento terminar fora de ordem. A vigência usa a publicação; não há comprovação do horário da captura no jogo.

O leitor também reconhece o layout de cartões do jogo: nomes à esquerda, etiquetas verdes de preço à direita, cabeçalho EM ALTA, subtítulos “VOCÊ TEM” e botões Vender. Lê nomes e preços em regiões separadas e confere a quantidade de cartões pelos subtítulos; cartão sem etiqueta ou preço ilegível impede a ativação inteira. `/tabela reler tabela_id:` permite à gerência reprocessar uma imagem pendente do período atual, preservando o resultado anterior na auditoria. Mensagens pendentes do período anterior exigem nova publicação; não são reativadas no novo período.

**Períodos:** America/Sao_Paulo, [06h,12h), [12h,18h), [18h,06h do dia seguinte). Exatamente na fronteira começa novo período. Validade é conferida em cada retirada, cálculo e confirmação de venda, além de aviso a cada período. Quando o bot volta após perder o reinício, preços antigos continuam bloqueados. Imagens em processamento interrompido ficam pendentes; extração que termina em outro período fica expirada. Na reconexão, o bot recupera mensagens com imagens do período atual, sem reprocessar mensagens já registradas. O aviso persistido/reconciliado não se repete em reconexões (mantenha o histórico do bot nos avisos).

**Trabalho:** painel ou `/retirar`, `/trabalho`, `/vender`, `/repasses` e `/entrega`. Na venda, informe ID de retirada aberta (listado no painel), confira o cálculo e confirme. A retirada inteira fecha; o total é calculado, sem alegar comprovação da venda no jogo. A venda usa preço vigente na confirmação e taxa/rendimento guardados na retirada. Mudança da tabela antes da confirmação exige novo cálculo. Alterar taxa/rendimento só afeta novas retiradas. Dinheiro é inteiro em centavos, taxa é Decimal, arredondamento de repasse é ROUND_HALF_UP; funcionário recebe o restante exato.

**Financeiro:** pendente → entrega informada → recebido. Quem informa entrega precisa indicar ID Discord do recebedor com cargo financeiro. `/recebimento` confirma/rejeita; rejeição exige motivo e retorna a pendente. Só confirmação credita o caixa, uma vez. `/financeiro` consulta saldo recebido e pendências; `/repasses funcionario:` consulta pendências individuais para gerência financeira. `/ajuste` lança valor assinado com motivo e `/estorno` compensa um lançamento uma única vez sem apagar histórico. Estorno não reabre venda/repasse nem gera automaticamente nova dívida: diferenças devem ser revisadas pela gerência. `/retirada_cancelar` resolve perdas/cancelamentos de retirada aberta com motivo; vendas fechadas exigem revisão financeira por ajuste. `/auditoria` consulta eventos recentes privados; a trilha completa está em SQLite.

## Railway

O Dockerfile já instala Tesseract e modelos por/eng. Publique este projeto como serviço Docker, crie um **volume montado em `/data`**, defina `DATABASE_PATH=/data/bukowski.sqlite3` e cadastre as variáveis Discord como segredos do serviço. Não defina `TESSERACT_CMD` do Windows no Railway. Use uma única instância/réplica, sem múltiplos processos escrevendo no mesmo banco. Não é necessário domínio público ou porta HTTP: o bot usa conexão de saída com o gateway Discord.

Migração inicial roda na inicialização, quando o volume está disponível. A tabela `migrations` registra a versão 1; não edite o esquema manualmente. Referências: [volumes Railway](https://docs.railway.com/volumes) e [Dockerfiles Railway](https://docs.railway.com/builds/dockerfiles). O volume persiste cadastros, painéis, eventos e financeiro; sem volume haverá perda após redeploy. Configure backups do volume; para cópia manual consistente, use a API `sqlite3.Connection.backup()` ou pare o bot antes de copiar banco/WAL. Restaure e verifique em ambiente isolado.

## Verificação

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python -m compileall -q bukowski
# Para OCR real no Windows, configure também a variável no processo:
$env:TESSERACT_CMD = 'C:\caminho\tesseract.exe'
$env:OCR_LANG = 'eng'
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Os testes cobrem autorização, falha/repetição de P1, concorrência de aprovação, identidades numéricas, histórico, restart, fronteiras de períodos, noite, reinício perdido, imagem sintética de referência, rejeição de imagem/texto inválido, OCR atravessando reinício, ordem de publicação, cálculos/arredondamento, snapshots de taxa/rendimento, propriedade da retirada, cancelamento, venda/recebimento duplicados, rejeição financeira e estorno. A imagem sintética usa os três pares fornecidos no pedido: não foi anexado um print real do jogo. O teste real é pulado explicitamente se Tesseract não estiver disponível.

Organização: `config.py` ambiente; `periods.py` fuso/períodos; `ocr.py` leitor/parser; `store.py` regras/transações/auditoria; `migrations/` migrações SQL versionadas e transacionais; `bot.py` Discord/painéis/comandos/eventos; `tests/` fluxos críticos. `requirements.lock.txt` fixa as versões usadas nos testes; `requirements.txt` descreve as dependências diretas. Instale dependências em ambiente próprio deste projeto para não alterar outros bots. Operações DB usam transação imediata/constraints; concessão do cargo é serializada em uma instância. Se o processo cair após P1 mas antes do commit, a próxima aprovação reaplica o mesmo cargo idempotentemente e finaliza o cadastro. O banco mantém histórico de quem saiu, mas desativa seu cadastro para novos trabalhos.
