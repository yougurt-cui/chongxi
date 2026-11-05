import math
import unittest

from services.pet_breed_catalog import catalog, confidence, normalize_identity, resolve_breed


class PetBreedCatalogTest(unittest.TestCase):
    def identity(self, species, breed, appearance=None):
        return normalize_identity({'animal_type': {'value': species}, 'breed': breed,
                                   'appearance': appearance or {}})

    def test_chinese_english_aliases_and_species_are_consistent(self):
        for species, english, chinese in [('cat', 'Persian', '波斯猫'),
                                          ('cat', 'domestic shorthair', '普通短毛家猫'),
                                          ('dog', 'Bichon Frise', '比熊犬'),
                                          ('dog', 'Maltese', '马尔济斯犬'),
                                          ('dog', 'Shih Tzu', '西施犬')]:
            self.assertEqual(resolve_breed(species, english)['value'], chinese)
        self.assertIsNone(resolve_breed('cat', 'Bichon Frise'))
        self.assertIsNone(resolve_breed('dog', 'Persian'))
        self.assertIsNone(resolve_breed('cat', '新编造的猫品种'))

    def test_calico_is_appearance_never_a_purebred_or_native_breed(self):
        result = self.identity('cat', {'value': '三花猫', 'confidence': .9},
                               {'length': {'value': 'short', 'confidence': .8}})
        self.assertEqual(result['breed']['value'], '猫品种待确认')
        self.assertEqual(result['appearance']['color']['value'], '黑白橘')
        self.assertEqual(result['appearance']['pattern']['value'], '三花')
        self.assertEqual(result['appearance']['length']['value'], '短毛')
        persian = self.identity('cat', {'value': 'Persian', 'confidence': .85},
                                {'pattern': {'value': 'calico', 'confidence': .9}})
        self.assertEqual(persian['breed']['value'], '波斯猫')
        self.assertEqual(persian['appearance']['pattern']['value'], '三花')

    def test_low_confidence_remains_candidate_and_aliases_are_deduplicated(self):
        result = self.identity('dog', {'value': 'Maltese', 'confidence': .45,
            'candidates': [{'value': '马尔济斯犬', 'confidence': .5},
                           {'value': 'Bichon Frise', 'confidence': .4},
                           {'value': 'Persian', 'confidence': .9}]})
        self.assertEqual(result['breed']['value'], '犬种待确认')
        self.assertEqual([x['value'] for x in result['breed']['candidates']], ['马尔济斯犬', '比熊犬'])
        self.assertEqual(result['breed']['candidates'][0]['confidence'], .5)
        self.assertTrue(result['breed']['requires_confirmation'])
        self.assertNotIn('混种', result['breed']['value'])

    def test_unknown_species_cannot_receive_cat_breed(self):
        result = self.identity('rabbit', {'value': 'Persian', 'confidence': 1})
        self.assertEqual(result['animal_type']['value'], 'unknown')
        self.assertEqual(result['breed']['value'], '')

    def test_catalog_ids_are_unique_and_confidence_is_finite(self):
        vocabulary = catalog()
        ids = [x['id'] for rows in vocabulary['breeds'].values() for x in rows]
        self.assertEqual(len(ids), len(set(ids)))
        for value in [math.inf, math.nan, 'invalid', None]:
            self.assertEqual(confidence(value), 0.)


if __name__ == '__main__':
    unittest.main()
