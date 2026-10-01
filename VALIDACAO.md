# Validação da primeira etapa

Executado em 01/10/2026 com Python 3.11.9, discord.py 2.7.1 e Tesseract 5.5.3 (modelos por+eng). Dependências isoladas em `.venv`; OCR local em `.tools/tesseract`, ignorado pelo Git. Não foi usado token Discord.

- 31 testes automatizados passaram, sem testes pulados, incluindo OCR real da imagem sintética de referência.
- Cadastro autorizado/não autorizado; concessão de P1 com falha e nova tentativa; decisão simultânea; saída durante aprovação; conflitos de documento e correção auditada.
- Persistência e migração inicial; instalação idempotente de painéis e recuperação de envio interrompido usando Discord simulado.
- Limites 06h/12h/18h, noite atravessando meia-noite, conversão UTC, bot retomando após reinício perdido.
- OCR dos três pares fornecidos; imagem inválida, texto ambíguo, autor não autorizado, conclusão após fronteira e publicação fora de ordem.
- Centavos e arredondamento HALF_UP; taxa obrigatória e limites; rendimento/taxa históricos; nova tabela exige nova confirmação.
- Venda/recebimento duplicados; recebimentos concorrentes em conexões SQLite distintas; rejeição seguida de nova entrega; cancelamento e estorno único.
- Compilação do código e `pip check` sem dependências quebradas.

Comando usado para testar OCR local (PowerShell):

```powershell
$env:TESSERACT_CMD = Join-Path (Get-Location) '.tools\tesseract\tesseract.exe'
$env:OCR_LANG = 'por+eng'
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Ainda dependem de configuração externa: token e IDs, intents no Developer Portal, cargos/hierarquia/permissões dos canais, taxa real da fazenda, volume Railway `/data` e teste integrado no servidor Discord. Nenhum deployment foi feito. O Dockerfile foi preparado, mas não foi construído neste ambiente (Docker indisponível). O print original do jogo não foi fornecido; valide seu layout em homologação. OCR de linhas com baixa confiança ou formato não reconhecido exige correção administrativa da tabela inteira.
