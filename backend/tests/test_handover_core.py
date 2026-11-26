import unittest

from src.services.handover_core import (HandoverBookBuilder, align_batch,
                                        keyset_page, snapshot_watermark)


class HandoverCoreTest(unittest.TestCase):
    def test_watermark_and_keyset_page_are_bounded(self):
        rows = [{'id': i} for i in range(1, 101)]
        self.assertEqual(snapshot_watermark(rows), 100)
        page = keyset_page(rows, after_id=20, limit=10, watermark=80)
        self.assertEqual([row['id'] for row in page], list(range(21, 31)))

    def test_align_requires_completion_evidence_and_signature(self):
        aligned = align_batch({
            'run_id': 7, 'execution_status': 'complete',
            'evidence_id': 3, 'digest_valid': True,
            'reviewer': 'reviewer', 'reviewed_at': '2026-11-26T00:00:00+00:00',
        })
        self.assertTrue(aligned['aligned'])
        missing = align_batch({'run_id': 8, 'execution_status': 'running'})
        self.assertFalse(missing['aligned'])
        self.assertTrue(any('完工' in issue for issue in missing['issues']))
        self.assertTrue(any('证据' in issue for issue in missing['issues']))
        self.assertTrue(any('签署' in issue for issue in missing['issues']))

    def test_builder_advances_only_cursor_without_remembering_ids(self):
        rows = [{'id': i, 'status': 'complete', 'evidence_id': i,
                 'digest_valid': True, 'reviewer': 'r', 'reviewed_at': 't'}
                for i in range(1, 11)]
        sizes = []

        def fetch(after, limit, watermark):
            page = keyset_page(rows, after, limit, watermark)
            sizes.append(len(page))
            return page

        builder = HandoverBookBuilder(
            watermark=10, page_size=3, fetch_page=fetch,
            persist=lambda items, checkpoint: None)
        checkpoint = builder.run()
        self.assertTrue(checkpoint.scan_complete)
        self.assertEqual(checkpoint.cursor, 10)
        self.assertEqual(sizes, [3, 3, 3, 1])


if __name__ == '__main__':
    unittest.main()
