import unittest

from dualmaker.ocr import correct_portuguese_ocr


class PortugueseOCRTests(unittest.TestCase):
    def test_known_ocr_errors_are_corrected_as_whole_words(self) -> None:
        text = "S6 agiientariam S60 agiientariamente"

        corrected = correct_portuguese_ocr(text)

        self.assertEqual(corrected, "Só aguentariam S60 agiientariamente")


if __name__ == "__main__":
    unittest.main()
