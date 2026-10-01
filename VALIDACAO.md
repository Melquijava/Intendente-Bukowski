# Validação da primeira etapa

Executado em 01/10/2026 com Python 3.11.9, discord.py 2.7.1 e Tesseract 5.5.3 (modelos por+eng). Dependências isoladas em `.venv`; OCR local em `.tools/tesseract`, ignorado pelo Git. Não foi usado token Discord.

- 59 testes automatizados passaram, sem testes pulados, incluindo OCR real das imagens sintéticas com Chuchu/Alho/Inhame e Junco/Romã/Pitanga, cabeçalho, rótulos e botões Vender.
- Cadastro autorizado/não autorizado; concessão de Peão com falha e nova tentativa; decisão simultânea; saída durante aprovação; conflitos de documento e correção auditada.
- Persistência e migração inicial; instalação idempotente de painéis e recuperação de envio interrompido usando Discord simulado.
- Limites 06h/12h/18h, noite atravessando meia-noite, conversão UTC, bot retomando após reinício perdido.
- OCR dos três pares fornecidos; imagem inválida, texto ambíguo, autor não autorizado, conclusão após fronteira e publicação fora de ordem.
- Regressão do layout reportado em 01/10: leitura separada de nomes/etiquetas verdes; cartão sem etiqueta ou preço ilegível rejeita a tabela inteira. Fixture reproduz o layout; a captura anexada no chat não estava disponível como arquivo local para execução do OCR original.
- Centavos e arredondamento HALF_UP; taxa obrigatória e limites; rendimento/taxa históricos; nova tabela exige nova confirmação.
- Venda/recebimento duplicados; recebimentos concorrentes em conexões SQLite distintas; rejeição seguida de nova entrega; cancelamento e estorno único.
- Compilação do código e `pip check` sem dependências quebradas.
- Reforma: cargo Peão e apelido a partir do documento; prioridade de Gerente; promoção/rebaixamento; somente nome truncado; hierarquia/dono do servidor; estado parcial e nova tentativa; correção cadastral; solicitação original atualizada com botões desabilitados.
- Migração versão 1 → 2: comparação de todos os campos antigos de cadastros, retiradas, vendas, pagamentos, caixa, histórico e painéis; backup consistente da versão 1 verificado.
- OCR sem VOCÊ TEM, sem quantidade e sem relógio; referência sintética em 900 e 685 pixels; categorias de OCR indisponível, formato inválido e falha de download; nenhum detalhe privado na resposta pública.
- Backend visual Gemini: implementação HTTP real, contrato de transporte testado com resposta controlada, chave ausente e resposta incompleta rejeitadas. Nenhuma chamada real ao provedor foi realizada.
- Embeds: conteúdos exportados em EMBEDS.md e examples/embeds.json; testes de limites de campos/caracteres e persistência. Não houve validação visual no Discord.
- Artes dos painéis: três PNGs gerados com ImageGen e inspecionados visualmente; integrados por anexos locais. Testes de arquivos PNG/tamanho e comparação das URLs de anexos convertidas pelo Discord para CDN, evitando reupload a cada atualização. A renderização final no Discord permanece sem validação.
- Disponibilidade OCR: caminho Windows inválido com executável disponível no PATH; configuração vazia não herda caminho global anterior; imagem real de fixture lida após fallback. Detecção local sem TESSERACT_CMD e com caminho inexistente confirmada pelo executável instalado. “VERDURAS” passa a ser texto acessório, sem virar produto. A instalação no ambiente remoto do usuário não foi inspecionada.

Comando usado para testar OCR local (PowerShell):

```powershell
$env:TESSERACT_CMD = Join-Path (Get-Location) '.tools\tesseract\tesseract.exe'
$env:OCR_LANG = 'por+eng'
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Ainda dependem de configuração externa: token e IDs, intents no Developer Portal, cargos/hierarquia/permissões dos canais, taxa real da fazenda, volume Railway `/data` e teste integrado no servidor Discord. Nenhum deployment foi feito. O Dockerfile foi preparado, mas não foi construído neste ambiente (Docker indisponível). O print original do jogo não foi fornecido; valide seu layout em homologação. OCR de linhas com baixa confiança ou formato não reconhecido exige correção administrativa da tabela inteira.
