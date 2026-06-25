def has_permission(user, permission_code):

    if user.role == "super_admin":
        return True

    for item in user.permissions:

        if item.permission.code == permission_code:
            return True

    return False