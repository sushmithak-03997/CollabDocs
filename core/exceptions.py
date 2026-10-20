from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


class SimulatedFailure(APIException):
    """Raised on demand (DEBUG only) to demonstrate a transaction rollback."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_detail = "Simulated failure: the transaction was rolled back."
    default_code = "simulated_failure"


def custom_exception_handler(exc, context):
    if isinstance(exc, IntegrityError):
        return Response(
            {"detail": "Conflict: this operation violates a database integrity constraint (duplicate record?)."},
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, ObjectDoesNotExist):
        return Response({"detail": "The requested resource was not found."}, status=status.HTTP_404_NOT_FOUND)
    return drf_exception_handler(exc, context)
