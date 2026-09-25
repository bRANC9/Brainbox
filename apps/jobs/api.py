"""REST API for the background job framework (admin/staff only).

Framework-only: no Brainbox domain imports, so this file is part of the
extractable core.
"""

from __future__ import annotations

from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from . import (
    engine,
    tasks_brainbox,  # noqa: F401  (registers the handlers)
)
from .models import Job, JobRun, JobTrigger
from .registry import get_job, list_jobs
from .scheduling import validate_schedule_config


class JobSerializer(serializers.ModelSerializer):
    schedule_description = serializers.CharField(read_only=True)
    config_errors = serializers.ListField(child=serializers.CharField(), read_only=True)
    registered = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = [
            "id",
            "name",
            "task_key",
            "schedule_kind",
            "schedule_config",
            "enabled",
            "timeout_sec",
            "max_retries",
            "next_run_at",
            "last_run_at",
            "schedule_description",
            "config_errors",
            "registered",
        ]
        read_only_fields = ["next_run_at", "last_run_at"]

    def get_registered(self, obj) -> bool:
        return get_job(obj.task_key) is not None

    def validate(self, attrs):
        kind = attrs.get("schedule_kind", getattr(self.instance, "schedule_kind", "interval"))
        config = attrs.get("schedule_config", getattr(self.instance, "schedule_config", {}))
        errors = validate_schedule_config(kind, config)
        if errors:
            raise serializers.ValidationError({"schedule_config": errors})
        return attrs


class JobRunSerializer(serializers.ModelSerializer):
    duration_seconds = serializers.FloatField(read_only=True)

    class Meta:
        model = JobRun
        fields = [
            "id",
            "run_id",
            "job",
            "task_key",
            "status",
            "trigger",
            "attempt",
            "payload",
            "created_at",
            "started_at",
            "ended_at",
            "duration_ms",
            "duration_seconds",
            "worker",
            "error",
            "detail",
        ]
        read_only_fields = fields


class JobRunViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = JobRunSerializer

    def get_queryset(self):
        return JobRun.objects.select_related("job")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        run = self.get_object()
        if not engine.cancel_run(run):
            raise ValidationError("Only waiting/running runs can be cancelled.")
        run.refresh_from_db()
        return Response(JobRunSerializer(run).data)


class JobViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = JobSerializer
    search_fields = ["name", "task_key"]
    ordering_fields = ["name", "next_run_at", "last_run_at"]

    def get_queryset(self):
        return Job.objects.all()

    @action(detail=False, methods=["get"])
    def registry(self, request):
        return Response(
            {
                "tasks": [
                    {
                        "key": spec.key,
                        "description": spec.description,
                        "dedupe_key": spec.dedupe_key,
                    }
                    for spec in list_jobs()
                ]
            }
        )

    @action(detail=False, methods=["get"])
    def scheduler_status(self, request):
        return Response(engine.status())

    @action(detail=True, methods=["post"])
    def run_now(self, request, pk=None):
        job = self.get_object()
        run = engine.enqueue_run(
            job.task_key,
            trigger=JobTrigger.MANUAL,
            payload=request.data.get("payload") or {},
        )
        if run is None:
            return Response(
                {"detail": "An active run already covers this dedupe key."},
                status=status.HTTP_409_CONFLICT,
            )
        if str(request.data.get("wait", "")).lower() in {"1", "true"}:
            engine.run_pending_once()
            run.refresh_from_db()
        return Response(JobRunSerializer(run).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def runs(self, request, pk=None):
        job = self.get_object()
        return Response(JobRunSerializer(job.runs.all()[:50], many=True).data)
