from __future__ import annotations

import json
import unittest

from search_engine import (
    QueryAnd,
    QueryOr,
    QueryTerm,
    SearchQuerySyntaxError,
    parse_search_query,
    query_variants_for_profile,
    query_capabilities_for_profile,
    query_matches,
    score_match,
    site_search_query,
    translate_query_for_profile,
    translate_search_query,
)


class AdvancedSearchQueryTests(unittest.TestCase):
    def test_simple_multiword_query_keeps_legacy_exact_phrase_behavior(self):
        parsed = parse_search_query("community schools")

        self.assertTrue(parsed.legacy_simple)
        self.assertEqual(parsed.root, QueryTerm("community schools", phrase=True))
        self.assertTrue(query_matches(parsed, "Our COMMUNITY schools serve families."))
        self.assertFalse(query_matches(parsed, "Schools support the community."))

    def test_boolean_precedence_is_not_then_and_then_or(self):
        parsed = parse_search_query("alpha OR beta AND gamma")

        self.assertIsInstance(parsed.root, QueryOr)
        self.assertIsInstance(parsed.root.operands[1], QueryAnd)
        self.assertTrue(query_matches(parsed, "alpha only"))
        self.assertTrue(query_matches(parsed, "beta plus gamma"))
        self.assertFalse(query_matches(parsed, "beta only"))

    def test_parentheses_override_boolean_precedence(self):
        parsed = parse_search_query("(alpha OR beta) AND gamma")

        self.assertTrue(query_matches(parsed, "alpha and gamma"))
        self.assertTrue(query_matches(parsed, "beta and gamma"))
        self.assertFalse(query_matches(parsed, "alpha only"))

    def test_quoted_phrase_wildcards_and_not_are_applied_locally(self):
        parsed = parse_search_query('"school board" AND counsel* NOT draft')

        self.assertTrue(query_matches(parsed, "School Board expands counseling services."))
        self.assertTrue(query_matches(parsed, "school board counselor program"))
        self.assertFalse(query_matches(parsed, "board school counseling services"))
        self.assertFalse(query_matches(parsed, "Draft school board counseling plan"))

    def test_question_mark_matches_exactly_one_term_character(self):
        parsed = parse_search_query("bud?et AND polic*")

        self.assertTrue(query_matches(parsed, "Budget policies for 2027"))
        self.assertFalse(query_matches(parsed, "Budet policies for 2027"))

    def test_wildcards_match_within_a_term_not_inside_an_unrelated_word(self):
        self.assertTrue(query_matches("cat*", "Categorical funding"))
        self.assertFalse(query_matches("cat*", "Education funding"))
        self.assertTrue(query_matches("*tion", "Education funding"))

    def test_invalid_queries_have_safe_user_facing_errors(self):
        invalid_queries = [
            "",
            '"unterminated',
            "()",
            "budget AND",
            "budget OR OR audit",
            "budget)",
            "NOT draft",
            "*",
            "(" * 51 + "budget" + ")" * 51,
        ]

        for query in invalid_queries:
            with self.subTest(query=query):
                with self.assertRaises(SearchQuerySyntaxError) as raised:
                    parse_search_query(query)
                self.assertTrue(str(raised.exception))
                self.assertNotIn("Traceback", str(raised.exception))

    def test_provider_translation_preserves_only_supported_syntax(self):
        parsed = parse_search_query('budget AND ("school board" OR counsel*) AND NOT draft')

        self.assertEqual(
            translate_search_query(parsed, provider="Brave"),
            'budget AND ("school board" OR counsel) AND NOT draft',
        )
        self.assertEqual(
            translate_search_query(parsed, provider="SearchStax / Solr"),
            'budget AND ("school board" OR counsel*) AND NOT draft',
        )
        self.assertEqual(
            translate_search_query(parsed, provider="Algolia"),
            "budget school board counsel",
        )
        self.assertEqual(
            translate_search_query('"student counsel*"', provider="SearchStax / Solr"),
            "student counsel*",
        )

    def test_profile_discovery_capabilities_can_override_provider_defaults(self):
        profile = {
            "provider_guess": "Custom",
            "raw_discovery_json": json.dumps(
                {
                    "query_capabilities": {
                        "boolean_operators": True,
                        "parentheses": True,
                        "quoted_phrases": True,
                        "wildcards": True,
                        "negation": True,
                    }
                }
            ),
        }

        capabilities = query_capabilities_for_profile(profile)
        translated = translate_query_for_profile('policy* AND NOT "old policy"', profile)

        self.assertTrue(capabilities.wildcards)
        self.assertEqual(translated, 'policy* AND NOT "old policy"')

    def test_limited_provider_gets_bounded_or_query_variants(self):
        profile = {"provider_guess": "Algolia"}

        variants = query_variants_for_profile(
            'budget AND (audit OR counsel*) NOT draft',
            profile,
        )

        self.assertEqual(variants, ("budget audit", "budget counsel"))

    def test_brave_site_query_keeps_site_scope_and_advanced_expression(self):
        self.assertEqual(
            site_search_query("community schools", "https://www.example.org/path"),
            '"community schools" site:example.org',
        )
        self.assertEqual(
            site_search_query("budget OR audit*", "https://district.example/path"),
            "budget OR audit site:district.example",
        )

    def test_score_match_enforces_full_expression_and_reports_positive_terms(self):
        query = '"school board" AND (counsel* OR mental) NOT draft'
        matched = score_match(
            query,
            "Student Supports",
            ["School Board Update"],
            "The district expanded counseling services this year.",
            "https://district.example/news/supports",
            "text/html",
        )
        excluded = score_match(
            query,
            "Draft School Board Update",
            [],
            "A draft counseling proposal.",
            "https://district.example/drafts/supports",
            "text/html",
        )

        self.assertIsNotNone(matched)
        self.assertEqual(matched["matched_terms"], ["school board", "counsel*"])
        self.assertIn("counseling", matched["snippet"].casefold())
        self.assertIsNone(excluded)

    def test_phrase_does_not_match_across_separate_page_fields(self):
        matched = score_match(
            '"school board"',
            "School",
            ["Board"],
            "Meeting information",
            "https://district.example/meeting",
            "text/html",
        )

        self.assertIsNone(matched)


if __name__ == "__main__":
    unittest.main()
