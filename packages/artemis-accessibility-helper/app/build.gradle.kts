plugins {
    id("com.android.application")
}

android {
    namespace = "com.artemis.helper"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.artemis.helper"
        minSdk = 24
        targetSdk = 35
        versionCode = 6
        versionName = "1.2.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }
}

dependencies {
    // Pure standard Android SDK APIs (android.accessibilityservice, android.view.accessibility, org.json)
    // Zero external dependencies to maximize stability, guarantee compatibility, and keep APK under 30KB.
}
