import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import DatabricksError
from databricks.sdk.service import iam, sql

from databricks.labs.ucx.mixins.hardening import rate_limited
from databricks.labs.ucx.workspace_access.base import (
    Applier,
    Crawler,
    Destination,
    Permissions,
    logger,
)
from databricks.labs.ucx.workspace_access.groups import GroupMigrationState


@dataclass
class SqlPermissionsInfo:
    object_id: str
    request_type: sql.ObjectTypePlural


# This module is called redash to disambiguate from databricks.sdk.service.sql


class SqlPermissionsSupport(Crawler, Applier):
    def __init__(self, ws: WorkspaceClient, listings: list[Callable[..., list[SqlPermissionsInfo]]]):
        self._ws = ws
        self._listings = listings

    def is_item_relevant(self, item: Permissions, migration_state: GroupMigrationState) -> bool:
        mentioned_groups = [
            acl.group_name for acl in sql.GetResponse.from_dict(json.loads(item.raw)).access_control_list
        ]
        return any(g in mentioned_groups for g in [info.workspace.display_name for info in migration_state.groups])

    def get_crawler_tasks(self):
        for listing in self._listings:
            for item in listing():
                yield partial(self._crawler_task, item.object_id, item.request_type)

    def _get_apply_task(self, item: Permissions, migration_state: GroupMigrationState, destination: Destination):
        new_acl = self._prepare_new_acl(
            sql.GetResponse.from_dict(json.loads(item.raw)).access_control_list,
            migration_state,
            destination,
        )
        return partial(
            self._applier_task,
            object_type=sql.ObjectTypePlural(item.object_type),
            object_id=item.object_id,
            acl=new_acl,
        )

    def _safe_get_dbsql_permissions(self, object_type: sql.ObjectTypePlural, object_id: str) -> sql.GetResponse | None:
        try:
            return self._ws.dbsql_permissions.get(object_type, object_id)
        except DatabricksError as e:
            if e.error_code in ["RESOURCE_DOES_NOT_EXIST", "RESOURCE_NOT_FOUND", "PERMISSION_DENIED"]:
                logger.warning(f"Could not get permissions for {object_type} {object_id} due to {e.error_code}")
                return None
            else:
                raise e

    @rate_limited(max_requests=100)
    def _crawler_task(self, object_id: str, object_type: sql.ObjectTypePlural) -> Permissions | None:
        permissions = self._safe_get_dbsql_permissions(object_type=object_type, object_id=object_id)
        if permissions:
            return Permissions(
                object_id=object_id,
                object_type=object_type.value,
                raw=json.dumps(permissions.as_dict()),
            )

    @rate_limited(max_requests=30)
    def _applier_task(self, object_type: sql.ObjectTypePlural, object_id: str, acl: list[sql.AccessControl]):
        """
        Please note that we only have SET option (DBSQL Permissions API doesn't support UPDATE operation).
        This affects the way how we prepare the new ACL request.
        """
        self._ws.dbsql_permissions.set(object_type=object_type, object_id=object_id, access_control_list=acl)
        return True

    def _prepare_new_acl(
        self, acl: list[sql.AccessControl], migration_state: GroupMigrationState, destination: Destination
    ) -> list[sql.AccessControl]:
        """
        Please note the comment above on how we apply these permissions.
        """
        acl_requests: list[sql.AccessControl] = []

        for acl_request in acl:
            if acl_request.group_name in [g.workspace.display_name for g in migration_state.groups]:
                migration_info = migration_state.get_by_workspace_group_name(acl_request.group_name)
                assert (
                    migration_info is not None
                ), f"Group {acl_request.group_name} is not in the migration groups provider"
                destination_group: iam.Group = getattr(migration_info, destination)
                new_acl_request = dataclasses.replace(acl_request, group_name=destination_group.display_name)
                acl_requests.append(new_acl_request)
            else:
                # no changes shall be applied
                acl_requests.append(acl_request)

        return acl_requests


def redash_listing_wrapper(
    func: Callable[..., list], object_type: sql.ObjectTypePlural
) -> Callable[..., list[SqlPermissionsInfo]]:
    def wrapper() -> list[SqlPermissionsInfo]:
        for item in func():
            yield SqlPermissionsInfo(
                object_id=item.id,
                request_type=object_type,
            )

    return wrapper
