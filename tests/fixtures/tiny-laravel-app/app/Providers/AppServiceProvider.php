<?php
namespace App\Providers;

use App\Contracts\UserRepositoryInterface;
use App\Repositories\UserRepository;
use Illuminate\Support\ServiceProvider;

class AppServiceProvider extends ServiceProvider
{
    public function register(): void
    {
        $this->app->singleton(UserRepositoryInterface::class, UserRepository::class);
        $this->app->bind('user.cache', fn($app) => new \App\Services\UserCacheService($app));
    }

    public function boot(): void
    {
        // Boot logic
    }
}
