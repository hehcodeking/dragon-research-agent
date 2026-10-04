import unittest

from dragon_agent.auction_units import auction_volume_fields


class AuctionUnitTests(unittest.TestCase):
    def test_observed_lot_like_quantity_is_not_relabelled_as_shares(self):
        result = auction_volume_fields({"auction_price": 1239.53,
                                       "auction_volume": 161, "auction_amount": 19956433})
        self.assertIsNone(result["auction_volume"])
        self.assertEqual(result["raw_auction_volume"], 161)
        self.assertFalse(result["auction_volume_unit_verified"])

    def test_consistent_share_quantity_and_rounding_are_accepted(self):
        result = auction_volume_fields({"auction_price": 1239.53,
                                       "auction_volume": 16100, "auction_amount": 19956433})
        self.assertEqual(result["auction_volume"], 16100)
        self.assertTrue(result["auction_volume_unit_verified"])

    def test_missing_invalid_or_mismatched_unit_evidence_is_not_scored(self):
        for price, volume, amount in ((None, 100, 1000), (10, 100, None),
                                     (10, -100, -1000), (10, True, 10),
                                     (10, float("nan"), 1000), (10, 100, 2000)):
            with self.subTest(price=price, volume=volume, amount=amount):
                self.assertIsNone(auction_volume_fields({"auction_price": price,
                                  "auction_volume": volume, "auction_amount": amount})["auction_volume"])
