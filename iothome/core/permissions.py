from rest_framework.permissions import BasePermission


class IsEmailVerified(BasePermission):
    """Buying and controlling hardware requires a proven mailbox."""

    message = "Verify your email address first."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_email_verified)


class IsGadgetOwner(BasePermission):
    """Object-level check for anything hanging off a gadget."""

    message = "You do not own this gadget."

    def has_object_permission(self, request, view, obj):
        owner_id = getattr(obj, "owner_id", None)
        if owner_id is None:
            owner_id = getattr(getattr(obj, "gadget", None), "owner_id", None)
        return owner_id == request.user.id
