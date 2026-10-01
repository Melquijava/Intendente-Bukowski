import io
import re
import unicodedata
from decimal import Decimal
from typing import Protocol
from PIL import Image, ImageOps
import pytesseract

def product(value):
    return unicodedata.normalize("NFC", value.strip().casefold())

def parse_prices(text):
    """Fail closed: every nonempty OCR line must contain one unambiguous pair."""
    prices = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*([\wÀ-ÿ][\wÀ-ÿ /-]{0,79}?)\s*[:;\-]?\s*\$?\s*(\d+[.,]\d{2})\s*", line)
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

class Reader(Protocol):
    def read(self, data: bytes) -> tuple[str, dict[str, int]]: ...

class TesseractReader:
    def __init__(self, lang="por+eng", command=""):
        self.lang = lang
        if command:
            pytesseract.pytesseract.tesseract_cmd = command

    def _text(self, image, psm=6):
        image = ImageOps.grayscale(image)
        image = ImageOps.autocontrast(image.resize((image.width * 3, image.height * 3)))
        data = pytesseract.image_to_data(image, lang=self.lang, config=f"--psm {psm}", timeout=30, output_type=pytesseract.Output.DICT)
        lines, confidences = {}, []
        for index, word in enumerate(data['text']):
            if not word.strip():
                continue
            key = (data['block_num'][index], data['par_num'][index], data['line_num'][index])
            lines.setdefault(key, []).append(word)
            confidences.append(Decimal(str(data['conf'][index])))
        return '\n'.join(' '.join(words) for words in lines.values()), confidences

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
        folded = ''.join(c for c in unicodedata.normalize('NFD',label_text.casefold()) if not unicodedata.combining(c))
        card_count = len(re.findall(r'\bvoce\s+tem\b',folded))
        if card_count != len(groups):
            error = ValueError('Nem todos os cartões têm nome/preço legível; revisão da tabela inteira necessária.')
            error.raw_text = label_text
            raise error
        result, raw = {}, []
        for group in groups:
            top, bottom = group[0][0],group[-1][0]+1
            left,right = min(r[1] for r in group),max(r[2] for r in group)+1
            badge_height = bottom-top
            price_image = image.crop((max(0,left-3),max(0,top-3),min(width,right+3),min(height,bottom+3)))
            name_image = image.crop((int(width*.10),max(0,top-badge_height),int(width*.55),min(height,top+max(1,badge_height//2))))
            name_text,name_conf = self._text(name_image)
            price_text,price_conf = self._text(price_image,7)
            # Explicitly ignore known subtitle labels, never arbitrary unknown lines.
            names = []
            for line in name_text.splitlines():
                normalized = ''.join(c for c in unicodedata.normalize('NFD',line.casefold()) if not unicodedata.combining(c))
                if re.search(r'\b(ervas|frutas|legumes|vegetais)\b.*\bvoce\b',normalized):
                    continue
                if line.strip():
                    names.append(line.strip())
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
                error.raw_text = '\n'.join(raw)
                raise
        # Every detected card must succeed; nothing is returned on partial failure.
        return '\n'.join(raw),result

    def read(self, data):
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 16_000_000:
                raise ValueError("Imagem excede 16 milhões de pixels.")
            game = self._game_rows(image)
            if game is not None:
                return game
            text, confidences = self._text(image)
        try:
            if confidences and (min(confidences) < 40 or sum(confidences)/len(confidences) < 75):
                raise ValueError('OCR com confiança insuficiente; revisão administrativa necessária.')
            return text, parse_prices(text)
        except ValueError as error:
            error.raw_text = text
            raise
