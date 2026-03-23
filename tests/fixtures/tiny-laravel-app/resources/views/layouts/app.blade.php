<!DOCTYPE html>
<html>
<head>
    <title>@yield('title', 'App')</title>
</head>
<body>
    <x-nav-bar />
    @yield('content')
    @stack('scripts')
</body>
</html>
