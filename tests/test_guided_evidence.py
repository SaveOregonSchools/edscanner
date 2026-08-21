from __future__ import annotations

import unittest

from guided_search.evidence import (
    build_evidence_bundle,
    canonical_evidence_url,
    effectively_same_query,
)


class GuidedEvidenceTests(unittest.TestCase):
    def test_canonical_url_removes_tracking_but_keeps_meaningful_parameters(self):
        self.assertEqual(
            canonical_evidence_url(
                "HTTPS://District.Example:443/program/?b=2&utm_source=test&a=1#section"
            ),
            "https://district.example/program?a=1&b=2",
        )

    def test_round_metrics_deduplicate_prior_urls_and_sample_districts_diversely(self):
        children = [
            {
                "child_type": "search",
                "child_run_id": 10,
                "round_number": 1,
                "query_text": '"community schools"',
                "purpose": "exact phrase",
                "districts_planned": 3,
                "districts_searched": 3,
                "districts_failed": 0,
            },
            {
                "child_type": "search",
                "child_run_id": 20,
                "round_number": 2,
                "query_text": '"full-service community schools"',
                "purpose": "terminology variant",
                "districts_planned": 3,
                "districts_searched": 3,
                "districts_failed": 1,
            },
        ]
        results = [
            {
                "id": 1,
                "search_run_id": 10,
                "district_id": 1,
                "district_name": "Alpha",
                "state": "OR",
                "url": "https://alpha.example/program?utm_source=old",
                "title": "Alpha program",
                "snippet": "Community schools program",
                "score": 9,
                "search_source": "crawler",
                "matched_terms_json": '["community schools"]',
            },
            {
                "id": 2,
                "search_run_id": 20,
                "district_id": 1,
                "district_name": "Alpha",
                "state": "OR",
                "url": "https://alpha.example/program?utm_source=new",
                "title": "Alpha program duplicate",
                "snippet": "Same canonical page",
                "score": 12,
                "search_source": "district_search+fetch",
                "matched_terms_json": '["community schools"]',
            },
            {
                "id": 3,
                "search_run_id": 20,
                "district_id": 2,
                "district_name": "Beta",
                "state": "OR",
                "url": "https://beta.example/initiatives/community",
                "title": "Beta initiative",
                "snippet": "A full-service model",
                "score": 7,
                "search_source": "district_search+fetch",
                "matched_terms_json": '["full-service"]',
            },
            {
                "id": 4,
                "search_run_id": 20,
                "district_id": 3,
                "district_name": "Gamma",
                "state": "WA",
                "url": "https://gamma.example/news/launch",
                "title": "Gamma launch",
                "snippet": "A new community school coordinator",
                "score": 5,
                "search_source": "crawler",
                "matched_terms_json": '["community school"]',
            },
        ]

        bundle = build_evidence_bundle(children, results, round_number=2, sample_limit=2)

        self.assertEqual(bundle.metrics.unique_urls, 3)
        self.assertEqual(bundle.metrics.new_unique_urls, 2)
        self.assertEqual(bundle.metrics.districts_with_hits, 3)
        self.assertEqual(bundle.metrics.failures, 1)
        self.assertEqual(len({item.district_id for item in bundle.sample}), 2)
        self.assertTrue(all(item.is_new for item in bundle.sample))
        self.assertFalse(next(item for item in bundle.unique_items if item.district_id == 1).is_new)

    def test_effectively_same_query_is_conservative(self):
        self.assertTrue(
            effectively_same_query('"community schools"', "community schools")
        )
        self.assertFalse(
            effectively_same_query("community schools", "restorative justice")
        )

    def test_sample_reserves_space_for_every_query_run(self):
        children = [
            {
                "child_type": "search",
                "child_run_id": run_id,
                "round_number": 1,
                "query_text": f'"query {run_id}"',
                "purpose": f"purpose {run_id}",
                "districts_planned": 30,
                "districts_searched": 30,
                "districts_failed": 0,
            }
            for run_id in (10, 20, 30)
        ]
        results = [
            {
                "id": district_id,
                "search_run_id": 10,
                "district_id": district_id,
                "district_name": f"District {district_id}",
                "state": "OR",
                "url": f"https://district-{district_id}.example/high-volume",
                "title": f"High-volume result {district_id}",
                "snippet": "Evidence from the first query.",
                "score": 100 - district_id,
                "search_source": "crawler",
                "matched_terms_json": "[]",
            }
            for district_id in range(1, 31)
        ]
        results.extend(
            [
                {
                    "id": 101,
                    "search_run_id": 20,
                    "district_id": 1,
                    "district_name": "District 1",
                    "state": "OR",
                    "url": "https://district-1.example/second-query",
                    "title": "Second query result",
                    "snippet": "Lower-scoring evidence from the second query.",
                    "score": 1,
                    "search_source": "crawler",
                    "matched_terms_json": "[]",
                },
                {
                    "id": 102,
                    "search_run_id": 30,
                    "district_id": 2,
                    "district_name": "District 2",
                    "state": "OR",
                    "url": "https://district-2.example/third-query",
                    "title": "Third query result",
                    "snippet": "Lower-scoring evidence from the third query.",
                    "score": 0.5,
                    "search_source": "crawler",
                    "matched_terms_json": "[]",
                },
            ]
        )

        bundle = build_evidence_bundle(children, results, round_number=1, sample_limit=30)

        self.assertEqual({item.search_run_id for item in bundle.sample}, {10, 20, 30})
        self.assertEqual(len(bundle.sample), 30)


if __name__ == "__main__":
    unittest.main()
