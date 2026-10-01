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

    def read(self, data):
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 16_000_000:
                raise ValueError("Imagem excede 16 milhões de pixels.")
            image = ImageOps.grayscale(image)
            data = pytesseract.image_to_data(image, lang=self.lang, config="--psm 6", timeout=30, output_type=pytesseract.Output.DICT)
            lines, confidences = {}, []
            for index, word in enumerate(data['text']):
                if not word.strip():
                    continue
                key = (data['block_num'][index], data['par_num'][index], data['line_num'][index])
                lines.setdefault(key, []).append(word)
                confidences.append(Decimal(str(data['conf'][index])))
            text = '\n'.join(' '.join(words) for words in lines.values())
        try:
            if confidences and (min(confidences) < 40 or sum(confidences)/len(confidences) < 75):
                raise ValueError('OCR com confiança insuficiente; revisão administrativa necessária.')
            return text, parse_prices(text)
        except ValueError as error:
            error.raw_text = text
            raise
