@extends('layouts.app')

@section('title', 'Posts')

@section('content')
    <h1>Posts</h1>
    @foreach($posts as $post)
        <x-post-card :post="$post" />
    @endforeach
    {{ $posts->links() }}
@endsection
