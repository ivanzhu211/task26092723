import unittest

from contentlib import MeshError, Store, ValidationError


def make_article_store():
    s = Store()
    m = s.create_mesh("article", [
        {"name": "title", "type": "short-text", "required": True},
        {"name": "body", "type": "long-text"},
        {"name": "summary", "type": "short-text", "default": ""},
    ])
    return s, m


class TestSchemaEvolutionChain(unittest.TestCase):
    def test_rename_delete_readd_swap_rollback(self):
        s, m = make_article_store()
        # 修订 1
        e, rev1 = s.save_entry(m, {
            "title": "Hello",
            "body": "a very long body",
            "summary": "short sum",
        })
        self.assertEqual(rev1, 1)

        # title 改名 headline
        s.change_schema(m, {"op": "rename_field", "from": "title", "to": "headline"})
        # 删除 body，再加一个同名的必填 short-text（带默认值）
        s.change_schema(m, {"op": "delete_field", "name": "body"})
        s.change_schema(m, {"op": "add_field", "field": {
            "name": "body", "type": "short-text", "required": True, "default": "N/A"}})
        # headline 与 summary 互换名字
        s.change_schema(m, {"op": "rename_field", "from": "headline", "to": "__swap"})
        s.change_schema(m, {"op": "rename_field", "from": "summary", "to": "headline"})
        s.change_schema(m, {"op": "rename_field", "from": "__swap", "to": "summary"})

        # 回滚到修订 1
        new_rev = s.rollback(e, 1)
        cur = s.current(e)
        # 旧 title 的值跟着字段身份走，现在落在 summary 名下
        self.assertEqual(cur["summary"], "Hello")
        # 旧 summary 的值现在落在 headline 名下
        self.assertEqual(cur["headline"], "short sum")
        # 新 body 是新字段，取默认值；旧 body 的长文本被丢弃
        self.assertEqual(cur["body"], "N/A")

        # get_revision 仍按写入时的 schema 原样返回
        self.assertEqual(s.get_revision(e, 1), {
            "title": "Hello", "body": "a very long body", "summary": "short sum"})

        # diff 按字段身份：改名不算增删；删后重加的 body 是两个不同字段
        d = s.diff(e, 1, new_rev)
        self.assertEqual(d["changed"], {})
        self.assertEqual(d["added"], {"body": "N/A"})
        self.assertEqual(d["removed"], {"body": "a very long body"})


class TestLinkRollbackFailure(unittest.TestCase):
    def test_rollback_fails_when_link_target_deleted(self):
        s = Store()
        person = s.create_mesh("person", [
            {"name": "name", "type": "short-text", "required": True}])
        article = s.create_mesh("article", [
            {"name": "title", "type": "short-text", "required": True},
            {"name": "author", "type": "link", "link_mesh": "person"},
        ])
        p, _ = s.save_entry(person, {"name": "Ada"})
        a, _ = s.save_entry(article, {"title": "T1", "author": p})
        # 被引用时拒绝删除
        with self.assertRaises(MeshError):
            s.delete_entry(p)
        # 修订 2：不再引用 p
        s.save_entry(article, {"title": "T2", "author": None}, entry_id=a)
        s.delete_entry(p)  # 现在可以删了

        before_current = s.current(a)
        before_revs = s.revisions(a)
        # 回滚到修订 1：author 指向已删除条目，整体失败并指明字段
        with self.assertRaises(ValidationError) as ctx:
            s.rollback(a, 1)
        self.assertEqual(ctx.exception.field, "author")
        # 当前值与修订列表都没变
        self.assertEqual(s.current(a), before_current)
        self.assertEqual(s.revisions(a), before_revs)


class TestInvariants(unittest.TestCase):
    def test_failed_ops_leave_no_trace(self):
        s, m = make_article_store()
        e, _ = s.save_entry(m, {"title": "t", "body": "x" * 300, "summary": "s"})
        version_before = s._current_version(m)
        # 转 short-text：有条目当前值超长，拒绝
        with self.assertRaises(ValidationError):
            s.change_schema(m, {"op": "convert_type", "name": "body", "to": "short-text"})
        self.assertEqual(s._current_version(m), version_before)
        # 失败的 save 不产生修订
        revs_before = s.revisions(e)
        with self.assertRaises(ValidationError):
            s.save_entry(m, {"title": "y" * 300}, entry_id=e)
        self.assertEqual(s.revisions(e), revs_before)

    def test_revision_pruning_and_cross_version_rollback(self):
        s, m = make_article_store()
        e, _ = s.save_entry(m, {"title": "v1", "body": "b", "summary": "s"})
        for i in range(2, 31):
            s.save_entry(m, {"title": f"v{i}"}, entry_id=e)
        revs = s.revisions(e)
        self.assertEqual(len(revs), 25)
        self.assertEqual(revs[0], 6)  # 只保留最近 25 条
        # 跨版本回滚到现存最老修订是常态，不得拒绝
        s.change_schema(m, {"op": "rename_field", "from": "title", "to": "headline"})
        new_rev = s.rollback(e, revs[0])
        self.assertEqual(s.current(e)["headline"], "v6")
        self.assertEqual(len(s.revisions(e)), 25)


if __name__ == "__main__":
    unittest.main()
