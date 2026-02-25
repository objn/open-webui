"""
BigQuery-backed store for file, knowledge, knowledge_file, chat_file.
When BIGQUERY_ENABLED=true, use these tables on BigQuery only (no PostgreSQL).
"""

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from open_webui.config import BIGQUERY_ENABLED

log = logging.getLogger(__name__)


def _bq():
    from open_webui.integrations import bigquery_sync as bq

    return bq


class FilesTableBigQuery:
    """Files table backed by BigQuery only (no PostgreSQL)."""

    def insert_new_file(
        self, user_id: str, form_data: Any, db: Optional[Session] = None
    ) -> Optional[Any]:
        return _bq().insert_file_bq(user_id, form_data)

    def get_file_by_id(self, id: str, db: Optional[Session] = None) -> Optional[Any]:
        return _bq().get_file_by_id_bq(id)

    def get_file_by_id_and_user_id(
        self, id: str, user_id: str, db: Optional[Session] = None
    ) -> Optional[Any]:
        f = _bq().get_file_by_id_bq(id)
        if f and f.user_id == user_id:
            return f
        return None

    def get_file_metadata_by_id(
        self, id: str, db: Optional[Session] = None
    ) -> Optional[Any]:
        return _bq().get_file_metadata_by_id_bq(id)

    def get_files(self, db: Optional[Session] = None) -> list:
        return _bq().get_all_files_bq()

    def check_access_by_user_id(
        self, id: str, user_id: str, permission: str = "write", db: Optional[Session] = None
    ) -> bool:
        f = self.get_file_by_id(id, db=db)
        return f is not None and f.user_id == user_id

    def get_files_by_ids(
        self, ids: list[str], db: Optional[Session] = None
    ) -> list:
        return _bq().get_files_by_ids_bq(ids)

    def get_file_metadatas_by_ids(
        self, ids: list[str], db: Optional[Session] = None
    ) -> list:
        return _bq().get_file_metadatas_by_ids_bq(ids)

    def get_files_by_user_id(
        self, user_id: str, db: Optional[Session] = None
    ) -> list:
        return _bq().get_files_by_user_id_bq(user_id)

    def search_files(
        self,
        user_id: Optional[str] = None,
        filename: str = "*",
        skip: int = 0,
        limit: int = 100,
        db: Optional[Session] = None,
    ) -> list:
        return _bq().search_files_bq(user_id=user_id, filename=filename, skip=skip, limit=limit)

    def update_file_by_id(
        self, id: str, form_data: Any, db: Optional[Session] = None
    ) -> Optional[Any]:
        return _bq().update_file_by_id_bq(id, form_data)

    def update_file_hash_by_id(
        self, id: str, hash_val: Optional[str], db: Optional[Session] = None
    ) -> Optional[Any]:
        return _bq().update_file_hash_by_id_bq(id, hash_val)

    def update_file_data_by_id(
        self, id: str, data: dict, db: Optional[Session] = None
    ) -> Optional[Any]:
        return _bq().update_file_data_by_id_bq(id, data)

    def update_file_metadata_by_id(
        self, id: str, meta: dict, db: Optional[Session] = None
    ) -> Optional[Any]:
        return _bq().update_file_metadata_by_id_bq(id, meta)

    def delete_file_by_id(self, id: str, db: Optional[Session] = None) -> bool:
        return _bq().delete_file_by_id_bq(id)

    def delete_all_files(self, db: Optional[Session] = None) -> bool:
        return _bq().delete_all_files_bq()


class KnowledgeTableBigQuery:
    """Knowledge and knowledge_file tables backed by BigQuery only. AccessGrants still in PostgreSQL."""

    def _get_access_grants(self, knowledge_id: str, db: Optional[Session] = None) -> list:
        from open_webui.models.access_grants import AccessGrants

        return AccessGrants.get_grants_by_resource("knowledge", knowledge_id, db=db)

    def _to_knowledge_model(self, k: Any, db: Optional[Session] = None) -> Any:
        from open_webui.models.knowledge import KnowledgeModel

        data = k.model_dump() if hasattr(k, "model_dump") else k
        if isinstance(data, dict):
            data.setdefault("access_grants", self._get_access_grants(data.get("id", ""), db=db))
            return KnowledgeModel.model_validate(data)
        data.access_grants = self._get_access_grants(data.id, db=db)
        return data

    def insert_new_knowledge(
        self, user_id: str, form_data: Any, db: Optional[Session] = None
    ) -> Optional[Any]:
        from open_webui.internal.db import get_db_context
        from open_webui.models.access_grants import AccessGrants

        k = _bq().insert_knowledge_bq(user_id, form_data)
        if k and getattr(form_data, "access_grants", None) is not None:
            with get_db_context(db) as session:
                AccessGrants.set_access_grants(
                    "knowledge", k.id, form_data.access_grants, db=session
                )
        return self._to_knowledge_model(k, db=db) if k else None

    def get_knowledge_by_id(self, id: str, db: Optional[Session] = None) -> Optional[Any]:
        k = _bq().get_knowledge_by_id_bq(id)
        return self._to_knowledge_model(k, db=db) if k else None

    def update_knowledge_by_id(
        self, id: str, form_data: Any, db: Optional[Session] = None
    ) -> Optional[Any]:
        from open_webui.internal.db import get_db_context
        from open_webui.models.access_grants import AccessGrants

        k = _bq().update_knowledge_by_id_bq(id, form_data)
        if k and hasattr(form_data, "access_grants") and form_data.access_grants is not None:
            with get_db_context(db) as session:
                AccessGrants.set_access_grants("knowledge", id, form_data.access_grants, db=session)
        return self._to_knowledge_model(k, db=db) if k else None

    def add_file_to_knowledge_by_id(
        self,
        knowledge_id: str,
        file_id: str,
        user_id: str,
        db: Optional[Session] = None,
    ) -> Optional[Any]:
        return _bq().add_knowledge_file_bq(knowledge_id, file_id, user_id)

    def remove_file_from_knowledge_by_id(
        self, knowledge_id: str, file_id: str, db: Optional[Session] = None
    ) -> bool:
        return _bq().remove_knowledge_file_bq(knowledge_id, file_id)

    def get_files_by_id(
        self, knowledge_id: str, db: Optional[Session] = None
    ) -> list:
        from open_webui.models.files import Files

        kf_list = _bq().get_knowledge_files_by_knowledge_id_bq(knowledge_id)
        file_ids = [kf.file_id for kf in kf_list]
        return Files.get_files_by_ids(file_ids, db=db) if file_ids else []

    def get_file_metadatas_by_id(
        self, knowledge_id: str, db: Optional[Session] = None
    ) -> list:
        from open_webui.models.files import Files

        kf_list = _bq().get_knowledge_files_by_knowledge_id_bq(knowledge_id)
        file_ids = [kf.file_id for kf in kf_list]
        return Files.get_file_metadatas_by_ids(file_ids, db=db) if file_ids else []

    def get_knowledges_by_file_id(
        self, file_id: str, db: Optional[Session] = None
    ) -> list:
        return [self._to_knowledge_model(k, db=db) for k in _bq().get_knowledges_by_file_id_bq(file_id)]

    def delete_knowledge_by_id(self, id: str, db: Optional[Session] = None) -> bool:
        from open_webui.models.access_grants import AccessGrants

        AccessGrants.revoke_all_access("knowledge", id, db=db)
        return _bq().delete_knowledge_by_id_bq(id)

    def get_knowledge_bases(
        self, skip: int = 0, limit: int = 30, db: Optional[Session] = None
    ) -> list:
        from open_webui.models.knowledge import KnowledgeUserModel
        from open_webui.models.users import Users

        all_k = _bq().get_all_knowledges_bq()
        user_ids = list({k.user_id for k in all_k if k.user_id})
        users = Users.get_users_by_user_ids(user_ids, db=db) if user_ids else []
        users_dict = {u.id: u for u in users}
        result = []
        for k in all_k[skip : skip + limit]:
            km = self._to_knowledge_model(k, db=db)
            result.append(
                KnowledgeUserModel.model_validate(
                    {**km.model_dump(), "user": users_dict.get(k.user_id)}
                )
            )
        return result

    def get_knowledge_by_id_and_user_id(
        self, id: str, user_id: str, db: Optional[Session] = None
    ) -> Optional[Any]:
        k = self.get_knowledge_by_id(id, db=db)
        if not k:
            return None
        if k.user_id == user_id:
            return k
        from open_webui.models.groups import Groups
        from open_webui.models.access_grants import AccessGrants

        user_group_ids = {g.id for g in Groups.get_groups_by_member_id(user_id, db=db)}
        if AccessGrants.has_access(
            user_id=user_id,
            resource_type="knowledge",
            resource_id=k.id,
            permission="write",
            user_group_ids=user_group_ids,
            db=db,
        ):
            return k
        return None

    def check_access_by_user_id(
        self, id: str, user_id: str, permission: str = "write", db: Optional[Session] = None
    ) -> bool:
        knowledge = self.get_knowledge_by_id(id, db=db)
        if not knowledge:
            return False
        if knowledge.user_id == user_id:
            return True
        from open_webui.models.groups import Groups
        from open_webui.models.access_grants import AccessGrants

        user_group_ids = {g.id for g in Groups.get_groups_by_member_id(user_id, db=db)}
        return AccessGrants.has_access(
            user_id=user_id,
            resource_type="knowledge",
            resource_id=knowledge.id,
            permission=permission,
            user_group_ids=user_group_ids,
            db=db,
        )

    def get_knowledge_bases_by_user_id(
        self, user_id: str, permission: str = "write", db: Optional[Session] = None
    ) -> list:
        from open_webui.models.groups import Groups
        from open_webui.models.access_grants import AccessGrants

        knowledge_bases = self.get_knowledge_bases(db=db)
        user_group_ids = {g.id for g in Groups.get_groups_by_member_id(user_id, db=db)}
        return [
            kb
            for kb in knowledge_bases
            if kb.user_id == user_id
            or AccessGrants.has_access(
                user_id=user_id,
                resource_type="knowledge",
                resource_id=kb.id,
                permission=permission,
                user_group_ids=user_group_ids,
                db=db,
            )
        ]

    def update_knowledge_data_by_id(
        self, id: str, data: dict, db: Optional[Session] = None
    ) -> Optional[Any]:
        k = _bq().update_knowledge_data_by_id_bq(id, data)
        return self._to_knowledge_model(k, db=db) if k else None

    def search_knowledge_bases(
        self,
        user_id: str,
        filter: dict,
        skip: int = 0,
        limit: int = 30,
        db: Optional[Session] = None,
    ) -> Any:
        from open_webui.models.knowledge import KnowledgeListResponse, KnowledgeUserModel
        from open_webui.models.users import Users
        from open_webui.models.groups import Groups
        from open_webui.models.access_grants import AccessGrants

        all_k = _bq().get_all_knowledges_bq()
        filter_user_id = (filter or {}).get("user_id")
        if filter_user_id:
            user_group_ids = {g.id for g in Groups.get_groups_by_member_id(filter_user_id, db=db)}
            all_k = [
                k
                for k in all_k
                if k.user_id == filter_user_id
                or AccessGrants.has_access(
                    user_id=filter_user_id,
                    resource_type="knowledge",
                    resource_id=k.id,
                    permission="read",
                    user_group_ids=user_group_ids,
                    db=db,
                )
            ]
        query_key = (filter or {}).get("query") or ""
        view_option = (filter or {}).get("view_option") or ""
        if query_key:
            q = query_key.lower()
            all_k = [k for k in all_k if q in (k.name or "").lower() or q in (k.description or "").lower()]
        if view_option == "created":
            all_k = [k for k in all_k if k.user_id == user_id]
        elif view_option == "shared":
            all_k = [k for k in all_k if k.user_id != user_id]
        total = len(all_k)
        page = all_k[skip : skip + limit]
        user_ids = list({k.user_id for k in page if k.user_id})
        users = Users.get_users_by_user_ids(user_ids, db=db) if user_ids else []
        users_dict = {u.id: u for u in users}
        items = []
        for k in page:
            km = self._to_knowledge_model(k, db=db)
            items.append(
                KnowledgeUserModel.model_validate(
                    {**km.model_dump(), "user": users_dict.get(k.user_id)}
                )
            )
        return KnowledgeListResponse(items=items, total=total)

    def search_knowledge_files(
        self, filter: dict, skip: int = 0, limit: int = 30, db: Optional[Session] = None
    ) -> Any:
        from open_webui.models.knowledge import KnowledgeFileListResponse, FileUserResponse
        from open_webui.models.users import Users
        from open_webui.models.files import Files

        user_id = (filter or {}).get("user_id")
        if not user_id:
            return KnowledgeFileListResponse(items=[], total=0)
        kbs = self.get_knowledge_bases_by_user_id(user_id, permission="read", db=db)
        knowledge_ids = [kb.id for kb in kbs]
        knowledge_models = {kb.id: kb for kb in kbs}
        triples = []
        for kid in knowledge_ids:
            kf_list = _bq().get_knowledge_files_by_knowledge_id_bq(kid)
            for kf in kf_list:
                triples.append((kf.file_id, kf.user_id, kid))
        if not triples:
            return KnowledgeFileListResponse(items=[], total=0)
        file_ids = list(dict.fromkeys(t[0] for t in triples))
        files = Files.get_files_by_ids(file_ids, db=db)
        file_map = {f.id: f for f in files}
        rows = []
        for file_id, kf_user_id, kid in triples:
            f = file_map.get(file_id)
            k = knowledge_models.get(kid)
            if f and k:
                rows.append((f, kf_user_id, k))
        query_key = (filter or {}).get("query") or ""
        if query_key:
            q = query_key.lower()
            rows = [r for r in rows if q in (r[0].filename or "").lower()]
        rows.sort(key=lambda r: (r[0].updated_at or 0, r[0].id), reverse=True)
        total = len(rows)
        page = rows[skip : skip + limit]
        user_ids = list({r[1] for r in page if r[1]})
        users = Users.get_users_by_user_ids(user_ids, db=db) if user_ids else []
        users_dict = {u.id: u for u in users}
        items = []
        for f, kf_user_id, k in page:
            user_resp = users_dict.get(kf_user_id)
            coll = k.model_dump() if hasattr(k, "model_dump") else k
            items.append(
                FileUserResponse(
                    **f.model_dump(),
                    user=user_resp,
                    collection=coll,
                )
            )
        return KnowledgeFileListResponse(items=items, total=total)

    def search_files_by_id(
        self,
        knowledge_id: str,
        user_id: str,
        filter: dict,
        skip: int = 0,
        limit: int = 30,
        db: Optional[Session] = None,
    ) -> Any:
        from open_webui.models.knowledge import KnowledgeFileListResponse, FileUserResponse
        from open_webui.models.users import Users
        from open_webui.models.files import Files

        kf_list = _bq().get_knowledge_files_by_knowledge_id_bq(knowledge_id)
        view_option = (filter or {}).get("view_option") or ""
        if view_option == "created":
            kf_list = [kf for kf in kf_list if kf.user_id == user_id]
        elif view_option == "shared":
            kf_list = [kf for kf in kf_list if kf.user_id != user_id]
        file_ids = [kf.file_id for kf in kf_list]
        if not file_ids:
            return KnowledgeFileListResponse(items=[], total=0)
        files = Files.get_files_by_ids(file_ids, db=db)
        query_key = (filter or {}).get("query") or ""
        if query_key:
            q = query_key.lower()
            files = [f for f in files if q in (f.filename or "").lower()]
        order_by = (filter or {}).get("order_by") or "updated_at"
        direction = (filter or {}).get("direction") or "desc"
        is_asc = direction == "asc"
        if order_by == "name":
            files.sort(key=lambda f: (f.filename or "").lower(), reverse=not is_asc)
        elif order_by == "created_at":
            files.sort(key=lambda f: f.created_at or 0, reverse=not is_asc)
        else:
            files.sort(key=lambda f: f.updated_at or 0, reverse=not is_asc)
        total = len(files)
        page = files[skip : skip + limit]
        user_ids = list({f.user_id for f in page if f.user_id})
        users = Users.get_users_by_user_ids(user_ids, db=db) if user_ids else []
        users_dict = {u.id: u for u in users}
        items = [
            FileUserResponse(**f.model_dump(), user=users_dict.get(f.user_id))
            for f in page
        ]
        return KnowledgeFileListResponse(items=items, total=total)

    def reset_knowledge_by_id(self, id: str, db: Optional[Session] = None) -> Optional[Any]:
        if _bq().reset_knowledge_by_id_bq(id):
            return self.get_knowledge_by_id(id, db=db)
        return None

    def delete_all_knowledge(self, db: Optional[Session] = None) -> bool:
        from open_webui.models.access_grants import AccessGrants

        all_k = _bq().get_all_knowledges_bq()
        for k in all_k:
            kid = k.id if hasattr(k, "id") else k.get("id")
            if kid:
                AccessGrants.revoke_all_access("knowledge", kid, db=db)
        return _bq().delete_all_knowledge_bq()
