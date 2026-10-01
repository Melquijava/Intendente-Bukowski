# Atualização da identidade Bukowski

Pare a instância do bot e mantenha o mesmo banco/volume. Atualize os arquivos e dependências (`python -m pip install -r requirements.lock.txt`) e inicie novamente. **Não exclua o banco, o volume nem as tabelas.** A migração SQL 002 acrescenta estados de cargo/apelido, rastreamento das mensagens administrativas e categoria de erro OCR, preservando registros, IDs, retiradas, vendas, repasses, caixa, auditoria e IDs dos painéis. Antes de atualizar um banco versão 1, a aplicação faz backup consistente em `<banco>.pre-v2.backup.sqlite3`. Guarde esse arquivo em local privado. Não faça downgrade para código versão 1 após a migração.

## Cargos e apelidos

- `PEAO_ROLE_ID`: ID do cargo de entrada **Peão**. O nome P1 estava incorreto. Nenhum cargo é criado, apagado ou renomeado pelo bot.
- Compatibilidade: `P1_ROLE_ID` anterior ainda é aceito como alias se `PEAO_ROLE_ID` não estiver preenchido. Migre a variável no Railway ou `.env`; ela deve apontar para o cargo Peão real.
- `GERENTE_ROLE_ID=1555083746406047774`, fornecido pelo usuário. Prefixo G, prioridade 100; Peão usa P, prioridade 10.
- `NICKNAME_FORMAT="{prefix} | {name} | {document}"`: formato central, ajustável quando os exemplos definitivos forem fornecidos.
- `NICKNAME_ROLE_MAP`: opcional; substitui o mapa de cargos e prioridades. Exemplo JSON (substitua os IDs pelos reais): `[{"role_id":123,"prefix":"P","priority":10},{"role_id":456,"prefix":"G","priority":100}]`. Cargos sem prefixo não recebem abreviação inventada. Empates usam o menor ID para escolha determinística.

Peão: `P | João Ribeiro | 215`. Gerente: `G | Nay Bukowski | 419`. Os números representam **documentos**, nunca pombo. Só o nome é encurtado para respeitar 32 caracteres; se prefixo/documento não couberem, o bot exige revisão. O cadastro é a fonte dos dados. Não lê nomes/documentos a partir de apelidos.

Conceda **Gerenciar Cargos** e **Gerenciar Apelidos** e coloque o bot acima dos cargos e membros administrados. A hierarquia impede editar membros acima do bot, e o dono do servidor não pode ter o apelido alterado por ele. Promoção, rebaixamento, correção cadastral e reconexão sincronizam os apelidos; apelido correto não gera nova chamada de edição. Falhas ficam registradas em `/cadastros` e na auditoria. Use `/cadastro_tentar cadastro:` para contratação pendente e `/apelido_sincronizar cadastro:` para aprovados. Se Peão foi concedido mas o apelido falhou, esse sucesso parcial é guardado e novas operações de trabalho permanecem bloqueadas no backend até concluir a aprovação.

## Painéis e imagens de identidade

Execute `/instalar` para atualizar os três painéis **nas mensagens existentes**. A inicialização atualiza painéis já registrados sem criar cópias. Cada painel usa embed com verde profundo, ouro envelhecido ou couro, campos organizados, rodapé fixo e componentes persistentes. Trabalho e gerência são atualizados em até 30 segundos e logo após alterações relevantes. Solicitações anteriores são reconciliadas pelo histórico do canal administrativo.

Distintivo e faixa não foram fornecidos como arquivos. Configure `BRAND_BADGE_URL` e `BRAND_BANNER_URL` somente quando houver URLs HTTPS reais acessíveis ao Discord. Sem elas, os embeds usam títulos, cores, textos e campos, sem imagem fictícia. Discord permite cor lateral, imagem e componentes; não permite controlar fonte ou fundo de embed. Exemplos finais: [EMBEDS.md](EMBEDS.md) e [examples/embeds.json](examples/embeds.json). São exemplos dos conteúdos, **não capturas de renderização no Discord**.

## OCR e reconhecimento visual alternativo

Localmente o Tesseract 5.5.3 está em `.tools/tesseract/tesseract.exe`, com `por`, `eng` e `osd`. O ambiente privado local foi configurado para esse executável. No Railway use o executável instalado pelo Dockerfile; não leve o caminho do Windows para Linux. `/ocr_diagnostico` retorna configuração técnica privada sem token.

`OCR_BACKEND=tesseract` é o padrão local, sem API externa. Limite total por imagem: 45 segundos, com etapas de no máximo 15 segundos e duas leituras simultâneas. São aceitos ponto/vírgula e dinheiro em centavos; produtos/preços nunca são fixos. Nomes e etiquetas são associados por linha, acessórios ignorados, e falha em qualquer cartão deixa toda a tabela sob revisão. Não exige relógio, data, inventário, VOCÊ TEM ou texto de reinício. Extrações parciais podem aparecer apenas como **não validadas**, para revisão administrativa.

Alternativa real: `OCR_BACKEND=gemini`, `GEMINI_API_KEY=<chave privada>` e `VISION_MODEL=gemini-2.5-flash` (ajustável). Usa imagem inline e resposta JSON estruturada; respostas incompletas, duplicadas ou inválidas não são ativadas. O anexo é enviado ao provedor externo quando esse backend é escolhido. A API exige chave, disponibilidade/cota do modelo e pode ter **custo variável por uso**; nenhum serviço pago foi chamado nesta implementação. Veja [imagens na API Gemini](https://ai.google.dev/gemini-api/docs/image-understanding), [saída estruturada](https://ai.google.dev/gemini-api/docs/structured-output) e [preços do provedor](https://ai.google.dev/gemini-api/docs/pricing). Sem chave, o diagnóstico é “OCR indisponível”, sem simular resposta. Não há fallback pago silencioso.

## Validação e limites

Execute a suíte com `TESSERACT_CMD` configurado. Há teste de migração a partir do esquema original que compara todos os campos antigos, inclusive saldo de caixa, antes/depois. A captura real de referência não está acessível como arquivo local: o anexo novo contém somente o pedido em texto. Portanto, foram executados testes OCR reais em **imagens sintéticas**, sem alegar leitura da captura original. A aparência no Discord de testes também não foi validada: não foi fornecido um servidor de homologação. Não houve deploy ou mensagens enviadas ao Discord durante esta atualização.

Para homologar: registre Peão; aprove e confira cargo/apelido; promova a Gerente; rebaixe a Peão; corrija documento; teste hierarquia insuficiente e nova tentativa; publique o print original sem relógio; confira os três valores e limites de reinício; confirme entrega uma única vez. Registros financeiros anteriores devem permanecer iguais.

O `.env.example` continha token e configurações locais. Foram preservados no `.env` privado (ignorado pelo Git), e o exemplo foi limpo. Regenere o token antes de compartilhar/publicar o projeto, principalmente se a versão anterior foi enviada ao Git. A atualização não altera o histórico Git.
