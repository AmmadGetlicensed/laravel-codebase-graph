<?php
namespace App\Http\Controllers;

use App\Models\Post;
use Illuminate\Http\Request;

class PostController extends Controller
{
    /**
     * Display a paginated list of published posts.
     *
     * @return \Illuminate\View\View
     */
    public function index(): \Illuminate\View\View
    {
        $posts = Post::published()->with('user', 'tags')->paginate(10);
        return view('posts.index', compact('posts'));
    }

    public function show(Post $post): \Illuminate\View\View
    {
        return view('posts.show', compact('post'));
    }

    public function unusedMethod(): void
    {
        // This is dead code — nothing calls it
        $posts = Post::all();
    }
}
