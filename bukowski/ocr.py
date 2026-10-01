import io
import re
import unicodedata
import threading
import time
from decimal import Decimal
from typing import Protocol
from PIL import Image, ImageOps, UnidentifiedImageError
import pytesseract

def product(value):
    return unicodedata.normalize("NFC", value.strip().casefold())

class ReadingError(ValueError):
    def __init__(self, code, message, raw_text='', partial=None, diagnostic=''):
        super().__init__(message)
        self.code,self.raw_text,self.partial,self.diagnostic = code,raw_text,partial or {},diagnostic

def accessory(line):
    plain = ''.join(c for c in unicodedata.normalize('NFD',line.casefold()) if not unicodedata.combining(c)).strip()
    return bool(re.fullmatch(r'(?:em\s+alta|vender|(?:legumes|ervas|frutas|vegetais)\b.*|voce\s+tem\b.*|[\d\s:·.-]+)',plain))

def names_from(text):
    return [line.strip() for line in text.splitlines() if line.strip() and not accessory(line)]

def parse_prices(text):
    """Fail closed: every nonempty OCR line must contain one unambiguous pair."""
    prices = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*([\wÀ-ÿ][\wÀ-ÿ /-]{0,79}?)\s*[:;\-–—]?\s*\$?\s*(\d+[.,]\d{2})\s*", line)
        if not match:
            raise ValueError(f"Linha incompleta ou ambígua: {line[:100]}")
        name = product(match[1])
        cents = int(Decimal(match[2].replace(',', '.')) * 100)
        if cents <= 0 or cents > 100_000_000 or (name in prices and prices[name] != cents):
            raise ValueError("Preço inválido ou produto com preços conflitantes.")
        prices[name] = cents
    if not prices:
        raise ValueError("Nenhum produto reconhecido.")
    return prices

def parse_shop_text(text):
    pairs, pending = [],None
    for raw in text.splitlines():
        line = re.sub(r'\s+Vender\s*$','',raw.strip(),flags=re.I)
        if not line or accessory(line):
            continue
        if pending is not None:
            if not re.fullmatch(r'\$?\s*\d+[.,]\d{2}',line):
                raise ReadingError('incomplete','Produto sem preço legível.',text)
            pairs.append(f'{pending}: {line}')
            pending = None
        elif re.fullmatch(r'[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ /-]{0,79}',line):
            pending = line
        else:
            pairs.append(line)
    if pending is not None:
        raise ReadingError('incomplete','Produto sem preço legível.',text)
    return parse_prices('\n'.join(pairs))

class Reader(Protocol):
    def read(self, data: bytes) -> tuple[str, dict[str, int]]: ...

class TesseractReader:
    def __init__(self, lang="por+eng", command=""):
        self.lang = lang
        self._timing = threading.local()
        if command:
            pytesseract.pytesseract.tesseract_cmd = command

    def diagnose(self):
        try:
            version = str(pytesseract.get_tesseract_version()).splitlines()[0]
            languages = pytesseract.get_languages()
        except (pytesseract.TesseractNotFoundError, OSError):
            raise ReadingError('ocr_unavailable','O leitor não está instalado ou configurado. Revise TESSERACT_CMD.')
        if set(self.lang.split('+'))-set(languages):
            raise ReadingError('ocr_unavailable','Faltam idiomas configurados no Tesseract.',diagnostic=f'Idiomas disponíveis: {", ".join(languages)}')
        return {'version':version,'languages':languages}

    def _text(self, image, psm=6, variant='gray'):
        image = ImageOps.grayscale(image)
        image = ImageOps.autocontrast(image.resize((image.width * 3, image.height * 3)))
        if variant == 'threshold':
            image = image.point(lambda value: 255 if value > 150 else 0)
        elif variant == 'inverted':
            image = ImageOps.invert(image)
        remaining = getattr(self._timing,'deadline',time.monotonic()+45)-time.monotonic()
        if remaining <= 0:
            raise ReadingError('ocr_service','O limite total de leitura foi excedido. Envie uma imagem menor e nítida.')
        data = pytesseract.image_to_data(image, lang=self.lang, config=f"--psm {psm}", timeout=min(15,remaining), output_type=pytesseract.Output.DICT)
        lines, line_confidences = {}, {}
        for index, word in enumerate(data['text']):
            if not word.strip():
                continue
            key = (data['block_num'][index], data['par_num'][index], data['line_num'][index])
            lines.setdefault(key, []).append(word)
            line_confidences.setdefault(key,[]).append(Decimal(str(data['conf'][index])))
        confidences = [c for key,words in lines.items() if not accessory(' '.join(words)) for c in line_confidences[key]]
        return '\n'.join(' '.join(words) for words in lines.values()), confidences

    def _region(self, image, price=False):
        attempts = []
        for variant in ('gray','inverted','threshold'):
            text,conf = self._text(image,7 if price else 6,variant)
            attempts.append(text)
            if not conf or min(conf) < 40 or sum(conf)/len(conf) < 65:
                continue
            if price:
                if not re.fullmatch(r'\s*\$?\s*\d+[.,]\d{2}\s*',text):
                    continue
            elif len(names_from(text)) != 1:
                continue
            return text,conf
        raise ReadingError('incomplete','Não foi possível ler um produto ou seu preço com segurança.',raw_text='\n'.join(attempts))

    def _game_rows(self, image):
        """Locate green price badges; read names and prices separately from UI labels."""
        image = image.convert('RGB')
        width, height = image.size
        pixels = image.load()
        green_rows = []
        # The game places unit prices in green badges on the right of each card.
        for y in range(height):
            xs = [x for x in range(int(width*.55), width) if
                  (lambda r,g,b: g > 60 and g > r*1.25 and g > b*1.10)(*pixels[x,y])]
            if len(xs) >= max(8, width//100):
                green_rows.append((y,min(xs),max(xs)))
        groups = []
        for row in green_rows:
            if not groups or row[0] > groups[-1][-1][0]+2:
                groups.append([])
            groups[-1].append(row)
        groups = [g for g in groups if g[-1][0]-g[0][0] >= 5]
        if not groups:
            return None
        label_text,_ = self._text(image.crop((int(width*.10),0,int(width*.55),height)))
        card_count = len(names_from(label_text))
        if card_count != len(groups):
            error = ReadingError('incomplete','Nem todos os cartões têm nome/preço legível; revisão da tabela inteira necessária.')
            error.raw_text = label_text
            raise error
        result, raw = {}, []
        for group in groups:
            top, bottom = group[0][0],group[-1][0]+1
            left,right = min(r[1] for r in group),max(r[2] for r in group)+1
            badge_height = bottom-top
            price_image = image.crop((max(0,left-3),max(0,top-3),min(width,right+3),min(height,bottom+3)))
            name_image = image.crop((int(width*.10),max(0,top-badge_height),int(width*.55),min(height,top+max(1,badge_height//2))))
            try:
                name_text,name_conf = self._region(name_image)
                price_text,price_conf = self._region(price_image,True)
            except ReadingError as error:
                error.partial = result.copy()
                raise
            # Explicitly ignore known subtitle labels, never arbitrary unknown lines.
            names = names_from(name_text)
            raw.append(f'Nome: {name_text} | Preço: {price_text}')
            try:
                if len(names) != 1 or not re.fullmatch(r'[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ /-]{0,79}',names[0]):
                    raise ValueError('Nome do produto incompleto ou ambíguo no cartão.')
                if not price_conf or min(price_conf) < 40 or not name_conf or sum(name_conf)/len(name_conf) < 60:
                    raise ValueError('Produto/preço com confiança OCR insuficiente.')
                pair = parse_prices(f'{names[0]}: {price_text.strip()}')
                for name,cents in pair.items():
                    if name in result:
                        raise ValueError('Cartões duplicados; revisão administrativa necessária.')
                    result[name] = cents
            except ValueError as error:
                raise ReadingError('ambiguous_price' if 'duplicados' in str(error) else 'incomplete',str(error),'\n'.join(raw),result)
        # Every detected card must succeed; nothing is returned on partial failure.
        return '\n'.join(raw),result

    def read(self, data):
        self._timing.deadline = time.monotonic()+45
        try:
            return self._read(data)
        except ReadingError:
            raise
        except (pytesseract.TesseractNotFoundError,FileNotFoundError):
            raise ReadingError('ocr_unavailable','OCR indisponível. Confira instalação e TESSERACT_CMD.')
        except UnidentifiedImageError:
            raise ReadingError('unsupported_format','O anexo não é uma imagem suportada. Envie PNG, JPEG ou WebP.')
        except pytesseract.TesseractError:
            raise ReadingError('ocr_service','Tesseract falhou. Confira idiomas, permissões e instalação.')
        except RuntimeError:
            raise ReadingError('ocr_service','O leitor excedeu o tempo de resposta. Envie uma imagem menor e nítida.')
        except ValueError as error:
            code = 'ambiguous_price' if 'conflitantes' in str(error) else 'incomplete'
            raise ReadingError(code,str(error),getattr(error,'raw_text',''))

    def _read(self, data):
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ('PNG','JPEG','WEBP'):
                raise ReadingError('unsupported_format','Formato não suportado. Envie PNG, JPEG ou WebP.')
            if image.width * image.height > 16_000_000:
                raise ValueError("Imagem excede 16 milhões de pixels.")
            game = self._game_rows(image)
            if game is not None:
                return game
            text, confidences = self._text(image)
        try:
            if confidences and (min(confidences) < 40 or sum(confidences)/len(confidences) < 75):
                raise ValueError('OCR com confiança insuficiente; revisão administrativa necessária.')
            return text, parse_shop_text(text)
        except ValueError as error:
            error.raw_text = text
            raise
