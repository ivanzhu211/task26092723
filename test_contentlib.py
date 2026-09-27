import unittest

from contentlib import ContentLibError, Store


class SchemaEvolutionChainTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.create_mesh(
            "article",
            [
                {"name": "title", "type": "short-text", "required": True, "default": "untitled"},
                {"name": "body", "type": "long-text"},
                {"name": "summary", "type": "short-text"},
            ],
        )

    def test_rename_delete_readd_swap_rollback(self):
        s = self.store
        entry = s.save_entry(
            "article", {"title": "Hello", "body": "Long body text", "summary": "Short sum"}
        )
        rev1 = s.get_revision(entry, 1)
        self.assertEqual(rev1["schema_version"], 1)
        self.assertEqual(
            rev1["values"],
            {"title": "Hello", "body": "Long body text", "summary": "Short sum"},
        )

        s.change_schema("article", [{"op": "rename_field", "from": "title", "to": "headline"}])
        s.change_schema(
            "article",
            [
                {"op": "remove_field", "name": "body"},
                {
                    "op": "add_field",
                    "name": "body",
                    "type": "short-text",
                    "required": True,
                    "default": "default-body",
                },
            ],
        )
        s.change_schema(
            "article",
            [
                {"op": "rename_field", "from": "headline", "to": "summary"},
                {"op": "rename_field", "from": "summary", "to": "headline"},
            ],
        )

        # history is not rewritten by schema changes
        rev1_after = s.get_revision(entry, 1)
        self.assertEqual(rev1_after, rev1)

        new_rev = s.rollback(entry, 1)
        current = s.get_entry(entry)
        # old title value lands in the field now named "summary" (same identity)
        self.assertEqual(current["summary"], "Hello")
        # old summary value lands in the field now named "headline"
        self.assertEqual(current["headline"], "Short sum")
        # new "body" is a different field: old long-text value dropped, default used
        self.assertEqual(current["body"], "default-body")

        result = s.diff(entry, 1, new_rev)
        # renames are not reported as add/remove
        self.assertNotIn("title", result["removed"])
        self.assertNotIn("headline", result["added"])
        self.assertNotIn("summary", result["added"])
        self.assertEqual(result["changed"], {})
        # deleted+readded "body" shows as one removal plus one addition
        self.assertEqual(result["removed"], {"body": "Long body text"})
        self.assertEqual(result["added"], {"body": "default-body"})


class LinkRollbackFailureTest(unittest.TestCase):
    def test_rollback_fails_when_link_target_deleted(self):
        s = Store()
        s.create_mesh("author", [{"name": "name", "type": "short-text"}])
        s.create_mesh(
            "article",
            [
                {"name": "title", "type": "short-text"},
                {"name": "author", "type": "link", "link": "author"},
            ],
        )
        author_a = s.save_entry("author", {"name": "Alice"})
        author_b = s.save_entry("author", {"name": "Bob"})
        article = s.save_entry("article", {"title": "T1", "author": author_a})
        s.save_entry("article", {"title": "T2", "author": author_b}, entry=article)

        # cannot delete while referenced by a current value
        s.save_entry("article", {"author": author_a}, entry=article)
        with self.assertRaises(ContentLibError):
            s.delete_entry(author_a)
        s.save_entry("article", {"author": author_b}, entry=article)
        s.delete_entry(author_a)

        before = s.get_entry(article)
        with self.assertRaises(ContentLibError) as ctx:
            s.rollback(article, 1)
        self.assertEqual(ctx.exception.field, "author")
        # current value and revision list unchanged
        self.assertEqual(s.get_entry(article), before)
        self.assertEqual(s.latest_rev_no(article), 4)
        self.assertEqual(s.get_revision(article, 4)["values"]["title"], "T2")

    def test_failed_schema_change_leaves_no_version(self):
        s = Store()
        s.create_mesh("m", [{"name": "a", "type": "short-text"}])
        with self.assertRaises(ContentLibError):
            s.change_schema("m", [{"op": "add_field", "name": "b", "type": "number", "required": True}])
        entry = s.save_entry("m", {"a": "x"})
        self.assertEqual(s.get_revision(entry, 1)["schema_version"], 1)


class RetentionTest(unittest.TestCase):
    def test_only_last_25_revisions_kept(self):
        s = Store()
        s.create_mesh("m", [{"name": "n", "type": "number"}])
        entry = s.save_entry("m", {"n": 0})
        for i in range(1, 30):
            s.save_entry("m", {"n": i}, entry=entry)
        for rev in range(1, 6):
            with self.assertRaises(ContentLibError):
                s.get_revision(entry, rev)
        self.assertEqual(s.get_revision(entry, 30)["values"]["n"], 29)
        self.assertEqual(s.get_revision(entry, 6)["values"]["n"], 5)


class ConvertFieldTest(unittest.TestCase):
    def test_long_to_short_rejected_when_value_too_long(self):
        s = Store()
        s.create_mesh("m", [{"name": "body", "type": "long-text"}])
        ok = s.save_entry("m", {"body": "x" * 300})
        with self.assertRaises(ContentLibError) as ctx:
            s.change_schema("m", [{"op": "convert_field", "name": "body", "to": "short-text"}])
        self.assertEqual(ctx.exception.field, "body")
        # no new schema version was produced
        self.assertEqual(s.get_revision(ok, 1)["schema_version"], 1)
        # shorten values, then long -> short -> long both succeed
        s.save_entry("m", {"body": "short"}, entry=ok)
        v = s.change_schema("m", [{"op": "convert_field", "name": "body", "to": "short-text"}])
        self.assertEqual(v, 2)
        v = s.change_schema("m", [{"op": "convert_field", "name": "body", "to": "long-text"}])
        self.assertEqual(v, 3)


if __name__ == "__main__":
    unittest.main()
