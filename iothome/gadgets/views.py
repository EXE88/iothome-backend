from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from rest_framework import generics, status
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsEmailVerified

from . import presence, protocol
from .models import Command, Gadget, GadgetType
from .serializers import (
    CommandSerializer,
    GadgetSerializer,
    GadgetTypeSerializer,
    ProvisioningSerializer,
)


class OwnedGadgetMixin:
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def owned_gadgets(self):
        return Gadget.objects.select_related(
            "gadget_type", "product"
        ).prefetch_related("gadget_type__capabilities").filter(owner=self.request.user)

    def get_gadget(self):
        return generics.get_object_or_404(
            self.owned_gadgets(), uid=self.kwargs["uid"]
        )


class GadgetListView(OwnedGadgetMixin, generics.ListAPIView):
    serializer_class = GadgetSerializer

    def get_queryset(self):
        return self.owned_gadgets()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # One Redis pipeline for the whole page instead of a call per row.
        uids = [str(uid) for uid in self.get_queryset().values_list("uid", flat=True)]
        context["online_uids"] = presence.online_uids(uids)
        return context


class GadgetDetailView(OwnedGadgetMixin, generics.RetrieveUpdateAPIView):
    serializer_class = GadgetSerializer
    lookup_field = "uid"

    def get_queryset(self):
        return self.owned_gadgets()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["online_uids"] = presence.online_uids([str(self.kwargs["uid"])])
        return context


class GadgetCommandsView(OwnedGadgetMixin, generics.ListAPIView):
    """Audit trail. Commands are *issued* over the WebSocket, where each one
    carries its own signature; this endpoint is read-only on purpose."""

    serializer_class = CommandSerializer

    def get_queryset(self):
        return Command.objects.filter(gadget=self.get_gadget())


class GadgetTypeListView(generics.ListAPIView):
    serializer_class = GadgetTypeSerializer
    permission_classes = [IsAuthenticated]
    queryset = GadgetType.objects.prefetch_related("capabilities").all()


class ProvisioningView(generics.RetrieveAPIView):
    """Assembly-line view of a unit: secret key and Wi-Fi credentials.

    Staff only — this is the one place those secrets leave the database.
    """

    serializer_class = ProvisioningSerializer
    permission_classes = [IsAdminUser]
    lookup_field = "uid"
    queryset = Gadget.objects.select_related("gadget_type", "owner")

    def retrieve(self, request, *args, **kwargs):
        gadget = self.get_object()
        response = super().retrieve(request, *args, **kwargs)
        if gadget.status == Gadget.STATUS_AWAITING_ASSEMBLY:
            gadget.status = Gadget.STATUS_ASSEMBLED
            gadget.save(update_fields=["status"])
        return response


class DisableGadgetView(OwnedGadgetMixin, APIView):
    """Let an owner kill a unit's access — e.g. after it is lost or resold."""

    def post(self, request, uid):
        gadget = self.get_gadget()
        gadget.status = Gadget.STATUS_DISABLED
        gadget.save(update_fields=["status"])
        presence.mark_offline(gadget.uid)
        # Kick the live socket too; the status check only guards new handshakes.
        async_to_sync(get_channel_layer().group_send)(
            presence.device_group(gadget.uid),
            {"type": protocol.EVENT_DEVICE_DISCONNECT, "reason": "disabled"},
        )
        return Response(
            {"detail": "Gadget disabled. It can no longer connect."},
            status=status.HTTP_200_OK,
        )
