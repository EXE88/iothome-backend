import logging
from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsEmailVerified

from .models import Order, Product
from .serializers import OrderCreateSerializer, OrderSerializer, ProductSerializer
from .services import CheckoutError, create_order, finalize_payment, start_payment

logger = logging.getLogger("iothome.purchases")


class ProductListView(generics.ListAPIView):
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]
    queryset = Product.objects.select_related("gadget_type").filter(is_active=True)


class ProductDetailView(generics.RetrieveAPIView):
    serializer_class = ProductSerializer
    permission_classes = [AllowAny]
    lookup_field = "slug"
    queryset = Product.objects.select_related("gadget_type").filter(is_active=True)


class OrderListView(generics.ListAPIView):
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated, IsEmailVerified]

    def get_queryset(self):
        return (
            Order.objects.select_related("product", "product__gadget_type")
            .prefetch_related("payments")
            .filter(user=self.request.user)
        )


class OrderDetailView(generics.RetrieveAPIView):
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated, IsEmailVerified]
    lookup_field = "uid"

    def get_queryset(self):
        return (
            Order.objects.select_related("product", "product__gadget_type")
            .prefetch_related("payments")
            .filter(user=self.request.user)
        )


class CheckoutView(APIView):
    """Create the order and hand the browser a gateway URL to redirect to."""

    permission_classes = [IsAuthenticated, IsEmailVerified]

    def post(self, request):
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            order = create_order(
                user=request.user,
                product_id=data["product"],
                quantity=data["quantity"],
                wifi_ssid=data["wifi_ssid"],
                wifi_password=data["wifi_password"],
                receiver_name=data["receiver_name"],
                receiver_phone=data["receiver_phone"],
                shipping_address=data["shipping_address"],
                postal_code=data.get("postal_code", ""),
            )
            payment, payment_url = start_payment(
                order, settings.ZARINPAL_CALLBACK_URL
            )
        except CheckoutError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )

        return Response(
            {
                "order": OrderSerializer(order).data,
                "payment_url": payment_url,
                "authority": payment.authority,
            },
            status=status.HTTP_201_CREATED,
        )


class PaymentVerifyView(APIView):
    """Callback target for Zarinpal.

    The gateway sends the *browser* here, not an API client, so the response
    is a redirect back to the frontend with the outcome in the query string.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        authority = request.query_params.get("Authority", "")
        gateway_status = request.query_params.get("Status", "")

        result = {"authority": authority}
        try:
            payment, gadgets = finalize_payment(
                authority=authority, gateway_status=gateway_status
            )
            result["status"] = payment.status
            result["order"] = str(payment.order.uid)
            result["ref_id"] = payment.ref_id
            result["gadgets"] = len(gadgets)
        except CheckoutError as exc:
            logger.warning("payment callback failed for %s: %s", authority, exc)
            result["status"] = "failed"
            result["detail"] = str(exc)

        return redirect(f"{settings.FRONTEND_PAYMENT_RESULT_URL}?{urlencode(result)}")
